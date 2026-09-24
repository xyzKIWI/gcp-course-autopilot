"""Site client against the live mock platform (in-process HTTP server, no internet)."""
import unittest
from datetime import datetime, timedelta

import requests

from autopilot.site import Client, FormChanged, SiteError, SiteProfile, course_id_of, done, parse_cards, score
from mock_site.server import PASSWORD, TZ, USER, Platform, serve

SECRETS = {'SITE_USER': USER, 'SITE_PASSWORD': PASSWORD}.get
FAR = datetime(2099, 1, 1, tzinfo=TZ)


class MockSiteBase(unittest.TestCase):
    def setUp(self):
        day = datetime.now(TZ).date()
        self.now = [datetime(day.year, day.month, day.day, 13, 51, tzinfo=TZ)]
        self.platform = Platform(clock=lambda: self.now[0], day=day)
        self.platform.registrations['DEMO101'] = '100'
        self.server = serve(self.platform)
        self.p = SiteProfile(base=f'http://127.0.0.1:{self.server.server_address[1]}/app/')
        self.c = Client(self.p, SECRETS, clock=lambda: self.now[0]).login()

    def tearDown(self):
        self.server.shutdown()


class Flow(MockSiteBase):
    def test_full_happy_path(self):
        self.c.attendance('signin', 'DEMO101', '100', FAR)
        self.now[0] = self.now[0].replace(hour=15, minute=51)
        self.c.attendance('signout', 'DEMO101', '100', FAR)
        self.c.survey('DEMO101', FAR)
        paper = self.c.exam('DEMO101')
        self.assertEqual([q['no'] for q in paper['questions']], ['1', '2', '3'])
        self.assertTrue(self.c.submit_exam(paper, {'1': 'A', '2': 'A', '3': 'A'}, FAR))   # receipt seen
        card = self.c.cards()['DEMO101']
        self.assertTrue(all(done(self.p, card['fields'][s]) for s in ('signin', 'signout', 'survey')))
        self.assertEqual(score(self.p, card['fields']['exam']), 100)

    def test_wrong_serial_does_not_sign_in(self):
        self.c.attendance('signin', 'DEMO101', '999', FAR)
        self.assertEqual(self.c.cards()['DEMO101']['fields']['signin'], 'Pending')

    def test_deadline_refused_before_post(self):
        with self.assertRaises(SiteError) as ctx:
            self.c.attendance('signin', 'DEMO101', '100', self.now[0] - timedelta(seconds=1))
        self.assertEqual(ctx.exception.args[0], 'deadline_passed')
        self.assertNotIn(('POST', '/app/student/signin'), self.platform.log)

    def test_dry_run_logs_in_but_never_posts_forms(self):
        c = Client(self.p, SECRETS, dry_run=True, clock=lambda: self.now[0]).login()
        c.attendance('signin', 'DEMO101', '100', FAR)
        self.assertNotIn(('POST', '/app/student/signin'), self.platform.log)

    def test_changed_form_is_detected(self):
        original = self.c._get
        def tampered(path, authenticated=True):
            text, soup = original(path, authenticated)
            form = soup.find('form')
            if form is not None:
                extra = soup.new_tag('textarea', attrs={'name': 'course_id'})
                form.append(extra)
            return text, soup
        self.c._get = tampered
        with self.assertRaises(FormChanged):
            self.c.attendance('signin', 'DEMO101', '100', FAR)


class Parsing(unittest.TestCase):
    p = SiteProfile(base='https://example.test/app/')

    def card(self, cid='DEMO101', rows=None, href=None):
        rows = rows or [('Check-in', 'Pending'), ('Check-out', 'Pending'), ('Survey', 'Pending'), ('Test score', 'Pending')]
        trs = ''.join(f'<tr><th>{a}</th><td>{b}</td></tr>' for a, b in rows)
        href = href or f'/app/course/info?course_id={cid}'
        return f'<div class="card"><div class="serial">100</div><table>{trs}</table><a href="{href}">i</a></div>'

    def test_isolated_card_errors(self):
        broken = self.card('BAD1', rows=[('Check-in', 'Pending')])
        cards = parse_cards(self.p, broken + self.card())
        self.assertEqual(cards['BAD1']['error'], 'card_status_rows')
        self.assertNotIn('error', cards['DEMO101'])

    def test_duplicate_card(self):
        self.assertEqual(parse_cards(self.p, self.card() + self.card())['DEMO101']['error'], 'duplicate_card')

    def test_exact_course_info_link(self):
        self.assertIsNone(course_id_of(self.p, 'https://evil.test/app/course/info?course_id=DEMO101'))
        self.assertIsNone(course_id_of(self.p, '/app/course/info?course_id=DEMO101&course_id='))
        self.assertEqual(course_id_of(self.p, '/app/course/info?course_id=DEMO101'), 'DEMO101')

    def test_fail_closed_values(self):
        self.assertTrue(done(self.p, '2026-09-23 13:51:00'))
        self.assertFalse(done(self.p, 'Pending'))
        for bad in ('2026-02-30 10:00:00', 'Expired', None):
            with self.assertRaises(FormChanged):
                done(self.p, bad)
        with self.assertRaises(FormChanged):
            score(self.p, 'absent')


class Transport(unittest.TestCase):
    p = SiteProfile(base='https://example.test/app/')

    def test_post_307_or_follow_failure_is_unknown(self):
        c = Client(self.p, SECRETS)
        r307 = type('R', (), {'status_code': 307, 'url': self.p.base, 'headers': {'Location': '/x'}, 'text': ''})()
        c.session.post = lambda *a, **k: r307
        with self.assertRaises(SiteError) as ctx:
            c._send('student/signin', {})
        self.assertEqual(ctx.exception.args[0], 'post_result_unknown')

    def test_offsite_redirect_refused(self):
        c = Client(self.p, SECRETS)
        r = type('R', (), {'status_code': 302, 'url': self.p.base, 'headers': {'Location': 'https://evil.test/'}})()
        c.session.get = lambda *a, **k: r
        with self.assertRaises(SiteError):
            c._get('student/card')

    def test_network_error_before_post_is_distinguishable(self):
        c = Client(self.p, SECRETS)
        def boom(*a, **k):
            raise requests.ConnectionError()
        c.session.get = boom
        with self.assertRaises(SiteError) as ctx:
            c._get('student/card')
        self.assertEqual(ctx.exception.args[0], 'network_error')


if __name__ == '__main__':
    unittest.main()
