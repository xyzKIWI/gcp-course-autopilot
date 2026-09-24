"""Browser-helper guard predicates, registration robot and the config-driven entry point."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from autopilot import browser_helper as bh
from autopilot import register, runner
from autopilot.config import load_config
from autopilot.site import SiteProfile
from mock_site.server import PASSWORD, TZ, USER, Platform, serve

P = SiteProfile(base='https://example.test/app/')
B = P.base
F = 'application/x-www-form-urlencoded'


def guard(step, cid='DEMO101', serial='100'):
    return bh.Guard(P, SimpleNamespace(cid=cid, serial=serial, step=step, deadline='2099-01-01T00:00:00+00:00'))


class HelperGuard(unittest.TestCase):
    def test_signin_submission_scoped(self):
        g = guard('signin')
        ok = lambda body, m='POST', ct=F: g.submission_ok(m, B + 'student/signin', body, ct)   # noqa: E731
        self.assertTrue(ok('csrf_token=t&course_id=DEMO101&user=u&password=p&reg_id=100'))
        self.assertFalse(ok('course_id=OTHER&reg_id=100'))
        self.assertFalse(ok('course_id=DEMO101&reg_id=999'))
        self.assertFalse(ok('course_id=DEMO101&COURSE_ID=OTHER&reg_id=100'))
        self.assertFalse(ok('course_id=DEMO101&reg_id=100', m='PUT'))
        self.assertFalse(ok('course_id=DEMO101&reg_id=100', ct='application/json'))

    def test_survey_needs_learned_form(self):
        g = guard('survey')
        url = B + 'form/survey?course_id=DEMO101'
        body = 'csrf_token=T&form_id=7&course_echo=DEMO101&course_id=DEMO101&q1=5'
        self.assertFalse(g.submission_ok('POST', url, body, F))
        g.hidden = {'csrf_token': 'T', 'form_id': '7'}
        self.assertTrue(g.submission_ok('POST', url, body, F))
        self.assertFalse(g.submission_ok('POST', url, body.replace('course_echo=DEMO101', 'course_echo=X'), F))

    def test_reads_limited_to_step_pages_and_same_site(self):
        g = guard('signin')
        self.assertTrue(g.read_ok(B + 'student/signin-form?course_id=DEMO101', 'document'))
        self.assertFalse(g.read_ok(B + 'student/signin-form?course_id=OTHER', 'document'))
        self.assertFalse(g.read_ok(B + 'test/exam?course_id=DEMO101', 'document'))
        self.assertFalse(g.read_ok('https://cdn.test/x.js', 'script'))
        self.assertTrue(g.read_ok(B + 'static/app.js', 'script'))

    def test_classify_fail_closed(self):
        rows = [('Check-in', '2026-09-23 13:51:00'), ('Check-out', 'Pending'), ('Survey', 'Pending'), ('Test score', '60')]
        self.assertEqual(bh.classify(P, rows, 'signout'), 'todo')
        self.assertEqual(bh.classify(P, rows, 'exam'), 'todo')
        self.assertNotEqual(bh.classify(P, rows, 'signin'), 'todo')
        self.assertNotEqual(bh.classify(P, rows[:-1] + [('Test score', '100')], 'exam'), 'todo')
        self.assertNotEqual(bh.classify(P, rows + [('Test score', '100')], 'exam'), 'todo')
        self.assertNotEqual(bh.classify(P, None, 'exam'), 'todo')


class Workspace(unittest.TestCase):
    def setUp(self):
        day = datetime.now(TZ).date()
        self.now = [datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)]
        self.platform = Platform(clock=lambda: self.now[0], day=day)
        self.server = serve(self.platform)
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / 'secrets.env').write_text(f'SITE_USER={USER}\nSITE_PASSWORD={PASSWORD}\n')
        (self.dir / 'config.toml').write_text(
            '[paths]\nstate_dir = "state"\nregistrations = "registrations.json"\nsecrets_file = "secrets.env"\n'
            f'[site]\nbase = "http://127.0.0.1:{self.server.server_address[1]}/app/"\n'
            '[notify]\ntype = "stdout"\n[[answer.providers]]\ntype = "static"\nletter = "A"\n')
        self.cfg = load_config(self.dir / 'config.toml')

    def tearDown(self):
        self.server.shutdown()

    def test_register_once_and_never_resend_unconfirmed(self):
        sent = []
        out = register.run(self.cfg, now=self.now[0], send=sent.append)
        self.assertTrue(out[0].startswith('✅'))
        data = json.loads((self.dir / 'registrations.json').read_text())
        self.assertEqual(data['courses']['DEMO101']['serial'], '100')
        # an intent without confirmation is reported, not retried
        data['courses']['DEMO101'].pop('status')
        data['intents'] = {'DEMO101': self.now[0].isoformat()}
        self.platform.registrations.clear()
        (self.dir / 'registrations.json').write_text(json.dumps(data))
        out = register.run(self.cfg, now=self.now[0], send=sent.append)
        self.assertTrue(any('為避免重複報名' in line for line in out))
        self.assertEqual(self.platform.registrations, {})

    def test_register_refuses_form_for_another_course(self):
        original = register.Client._get
        def tampered(client, path, authenticated=True):
            text, soup = original(client, path, authenticated)
            if 'register-form' in path:
                soup.find('input', attrs={'name': 'course_id'})['value'] = 'OTHER'
            return text, soup
        with mock.patch.object(register.Client, '_get', tampered):
            out = register.run(self.cfg, now=self.now[0], send=lambda t: None)
        self.assertTrue(any('未送出' in line for line in out))
        self.assertEqual(self.platform.registrations, {})
        self.assertNotIn(('POST', '/app/student/register'), self.platform.log)

    def test_register_creates_state_dir_and_attempts_at_most_one(self):
        cfg = dict(self.cfg, paths=dict(self.cfg['paths'], registrations=str(self.dir / 'state' / 'reg.json')))
        self.platform.courses['DEMO102'] = dict(self.platform.courses['DEMO101'], title='Second')
        calls = []
        def failing(client, path, data, deadline):
            calls.append(path)
            raise register.SiteError('post_result_unknown')
        with mock.patch.object(register.Client, '_post', failing):
            out = register.run(cfg, now=self.now[0], send=lambda t: None)
        self.assertEqual(len(calls), 1)                       # the second course waits for the next run
        self.assertTrue((self.dir / 'state' / 'reg.json').exists())
        self.assertTrue(any('不會自動重送' in line for line in out))

    def test_main_status_and_dry_run_are_read_only(self):
        state_dir = self.dir / 'state'
        state_dir.mkdir()
        (state_dir / 'state.json').write_text('{broken')
        with mock.patch('sys.stdout'):
            runner.main(['--config', str(self.dir / 'config.toml'), '--status'])
            runner.main(['--config', str(self.dir / 'config.toml'), '--dry-run'])
        self.assertEqual(sorted(p.name for p in state_dir.iterdir()), ['state.json'])

    def test_main_quarantines_corrupt_state_and_blocks_exam(self):
        state_dir = self.dir / 'state'
        state_dir.mkdir()
        (state_dir / 'state.json').write_text('{broken')
        (self.dir / 'registrations.json').write_text(json.dumps({'courses': {}}))
        with mock.patch('sys.stdout'):
            self.assertEqual(runner.main(['--config', str(self.dir / 'config.toml')]), 0)
        self.assertTrue(list(state_dir.glob('state.corrupt-*.json')))
        self.assertIn('recovered_at', json.loads((state_dir / 'state.json').read_text()))

    def test_main_alerts_on_missing_or_malformed_registrations(self):
        sent = []
        with mock.patch.object(runner, 'make_notifier', lambda cfg, secret: (lambda t: sent.append(t) or True)), \
                mock.patch('sys.stdout'):
            runner.main(['--config', str(self.dir / 'config.toml')])
            runner.main(['--config', str(self.dir / 'config.toml')])          # deduplicated
            (self.dir / 'registrations.json').write_text('[]')
            runner.main(['--config', str(self.dir / 'config.toml')])
        self.assertEqual(sum('找不到報名狀態檔' in s for s in sent), 1)
        self.assertTrue(any('結構不對' in s for s in sent))

    def test_main_never_writes_registrations(self):
        reg = self.dir / 'registrations.json'
        reg.write_text(json.dumps({'courses': {}}))
        before = reg.read_bytes()
        with mock.patch('sys.stdout'):
            runner.main(['--config', str(self.dir / 'config.toml')])
        self.assertEqual(reg.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
