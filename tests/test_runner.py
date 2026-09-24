import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from autopilot import runner
from autopilot.site import SiteError, FormChanged, SiteProfile
from autopilot.runner import Tick, select_courses
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Etc/GMT-2')   # fixed UTC+2 for deterministic tests
PROFILE = SiteProfile(base='http://127.0.0.1:1/app/')

ST = '2026-10-07 13:51:02'

PW = 'S3cret-PW!'
SECRETS = {'SITE_USER': 'user@example.com', 'SITE_PASSWORD': PW, 'TELEGRAM_BOT_TOKEN': 'TGTOKEN123'}.get
PENDING = 'Pending'

DETAIL = {
    'title': 'Statistics for trials', 'mode': 'online',
    'checkin': '13:50–14:20', 'checkin_start': '2026-10-07T13:50:00+02:00', 'checkin_end': '2026-10-07T14:20:00+02:00',
    'checkout': '15:50–16:30', 'checkout_start': '2026-10-07T15:50:00+02:00', 'checkout_end': '2026-10-07T16:30:00+02:00',
}
COURSE = {'cid': 'DEMO2026008', 'serial': '123', **DETAIL}


def at(hhmm):
    h, m = map(int, hhmm.split(':'))
    return datetime(2026, 10, 7, h, m, tzinfo=TZ)


class FakeClient:
    """In-memory platform: posts flip card fields unless told to fail."""

    def __init__(self, fields=None, serial='123', fail=None, score_after=100, present=True):
        self.fields = dict(signin=PENDING, signout=PENDING, survey=PENDING, exam=PENDING)
        self.fields.update(fields or {})
        self.serial, self.fail, self.score_after, self.present = serial, fail or {}, score_after, present
        self.calls = []
        self.receipt = True
        self.card_error = None

    def cards(self):
        if self.card_error:
            return {'DEMO2026008': {'cid': 'DEMO2026008', 'error': self.card_error}}
        if not self.present:
            return {}
        return {'DEMO2026008': {'cid': 'DEMO2026008', 'serial': self.serial, 'fields': dict(self.fields),
                               'material': None}}

    def _act(self, step, value):
        self.calls.append(step)
        err = self.fail.get(step)
        if err:
            raise err
        self.fields[step] = value

    def attendance(self, kind, cid, serial, deadline):
        self._act(kind, ST)

    def survey(self, cid, deadline):
        self._act('survey', '2026-10-07 15:52:00')

    def exam(self, cid):
        self.calls.append('exam_fetch')
        if 'exam_fetch' in self.fail:
            raise self.fail['exam_fetch']
        return {'questions': [{'no': '1', 'options': {'A': 'a', 'B': 'b'}}]}

    def submit_exam(self, paper, answers, deadline):
        self.calls.append('exam_submit')
        if 'exam_submit_lost' in self.fail:          # POST outcome unknown, nothing recorded
            raise self.fail['exam_submit_lost']
        self.fields['exam'] = str(self.score_after.pop(0) if isinstance(self.score_after, list) else self.score_after)
        return self.receipt


class FakeAgy:
    def __init__(self, fixes=None, uncertain=None):
        self.fixes, self.uncertain = fixes, uncertain or []
        self.fallbacks, self.answers = [], 0

    def answer(self, course, paper, material, secret=None, **kw):
        self.answers += 1
        self.last_kw = kw
        if getattr(self, 'fail', None):
            raise self.fail
        return {'1': 'A'}, self.uncertain, 'provider-a'

    def fallback(self, course, step, secret=None, on_start=None, **kw):
        if on_start:
            on_start()
        self.fallbacks.append(step)
        if self.fixes is not None:
            self.fixes.fields[step] = '100' if step == 'exam' else ST
        return 'completed'


def tick(now, client, state=None, agy=None, dry=False, clock=None):
    sent = []
    t = Tick(state if state is not None else {}, SECRETS, now, dry_run=dry, client=client,
             agents=agy or FakeAgy(), send=lambda text: sent.append(text) or True, clock=clock or (lambda: now),
             profile=PROFILE)
    t.run([COURSE])
    return t, sent


class Windows(unittest.TestCase):
    def test_before_window_does_nothing(self):
        c = FakeClient()
        tick(at('13:50'), c)   # window opens +1 minute
        self.assertEqual(c.calls, [])

    def test_signin_inside_window(self):
        c = FakeClient()
        t, sent = tick(at('13:51'), c)
        self.assertEqual(c.calls, ['signin'])
        self.assertTrue(t.state['courses']['DEMO2026008']['steps']['signin']['done'])
        self.assertTrue(any('簽到完成' in s for s in sent))

    def test_signout_waits_for_its_window(self):
        c = FakeClient({'signin': ST})
        tick(at('15:00'), c)
        self.assertEqual(c.calls, [])

    def test_full_afternoon_chain(self):
        c = FakeClient({'signin': ST})
        t, sent = tick(at('15:51'), c)
        self.assertEqual(c.calls, ['signout', 'survey', 'exam_fetch', 'exam_submit'])
        steps = t.state['courses']['DEMO2026008']['steps']
        self.assertTrue(all(steps[s]['done'] for s in runner.STEPS))

    def test_expired_step_alerts_once(self):
        c = FakeClient()
        state = {}
        _, sent1 = tick(at('14:20'), c, state)
        _, sent2 = tick(at('14:21'), c, state)
        self.assertEqual(c.calls, [])
        self.assertEqual(len([s for s in sent1 if '已過時間窗' in s]), 1)
        self.assertEqual([s for s in sent2 if '已過時間窗' in s], [])


class Idempotency(unittest.TestCase):
    def test_already_signed_in_not_resent(self):
        c = FakeClient({'signin': '2026-10-07 13:50:30'})
        t, _ = tick(at('13:55'), c)
        self.assertEqual(c.calls, [])
        self.assertTrue(t.state['courses']['DEMO2026008']['steps']['signin']['done'])

    def test_serial_mismatch_blocks_posting(self):
        c = FakeClient(serial='999')
        tick(at('13:51'), c)
        self.assertEqual(c.calls, [])

    def test_missing_card_blocks_posting(self):
        c = FakeClient(present=False)
        tick(at('13:51'), c)
        self.assertEqual(c.calls, [])

    def test_dry_run_no_post_no_agy_no_notify(self):
        c, agy = FakeClient(), FakeAgy()
        t, sent = tick(at('13:51'), c, agy=agy, dry=True)
        self.assertEqual(c.calls, [])
        self.assertEqual((agy.fallbacks, agy.answers, sent), ([], 0, []))
        self.assertIn('DEMO2026008: would signin', t.plan)


class Fallback(unittest.TestCase):
    def test_form_changed_goes_to_agy_once(self):
        c = FakeClient(fail={'signin': FormChanged('signin_form_shape')})
        agy = FakeAgy(fixes=c)
        state = {}
        t, sent = tick(at('13:51'), c, state, agy)
        self.assertEqual(agy.fallbacks, ['signin'])
        self.assertTrue(state['courses']['DEMO2026008']['steps']['signin']['done'])
        self.assertTrue(any('代理人' in s for s in sent))

    def test_network_errors_retry_then_agy(self):
        c = FakeClient(fail={'signin': SiteError('network_error')})
        agy, state = FakeAgy(), {}
        for minute in ('13:51', '13:52', '13:53', '13:54'):
            tick(at(minute), c, state, agy)
        self.assertEqual(c.calls.count('signin'), 3)
        self.assertEqual(agy.fallbacks, ['signin'])
        _, sent = tick(at('13:55'), c, state, agy)
        self.assertEqual(agy.fallbacks, ['signin'])   # never a second fallback

    def test_agy_failure_alerts(self):
        c = FakeClient(fail={'signin': FormChanged(ST)})
        _, sent = tick(at('13:51'), c, {}, FakeAgy())
        self.assertTrue(any('請立刻手動處理' in s for s in sent))


class Exam(unittest.TestCase):
    def test_pass_is_never_retaken(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '80'})
        t, _ = tick(at('15:55'), c)
        self.assertNotIn('exam_submit', c.calls)
        self.assertTrue(t.state['courses']['DEMO2026008']['steps']['exam']['done'])

    def test_low_score_retried_once_then_alert(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}, score_after=[60, 70])
        state = {}
        for minute in ('15:55', '15:56', '15:57'):
            _, sent = tick(at(minute), c, state)
        self.assertEqual(c.calls.count('exam_submit'), 2)
        self.assertTrue(any('手動重考' in s for s in sent))

    def test_low_then_pass(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}, score_after=[60, 100])
        state = {}
        tick(at('15:55'), c, state)
        tick(at('15:56'), c, state)
        self.assertTrue(state['courses']['DEMO2026008']['steps']['exam']['done'])

    def test_uncertain_reported(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST})
        _, sent = tick(at('15:55'), c, agy=FakeAgy(uncertain=['1']))
        self.assertTrue(any('不確定題：1' in s for s in sent))


class Secrets(unittest.TestCase):
    def test_password_never_in_state_or_messages(self):
        c = FakeClient(fail={'signin': SiteError('boom ' + PW)})
        state = {}
        sent_all = []
        for minute in ('13:51', '13:52', '13:53', '13:54'):
            _, sent = tick(at(minute), c, state, FakeAgy())
            sent_all += sent
        blob = json.dumps(state, ensure_ascii=False) + ''.join(sent_all)
        self.assertNotIn(PW, blob)
        self.assertNotIn('TGTOKEN123', blob)


class Lifecycle(unittest.TestCase):
    def test_first_seen_after_class_is_silent(self):
        c = FakeClient(present=False)
        _, sent = tick(at('18:00'), c)
        self.assertEqual((c.calls, sent), ([], []))

    def test_closed_course_stops_polling(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '100'})
        state = {}
        tick(at('09:00'), c, state)
        tick(at('16:31'), c, state)
        self.assertTrue(state['courses']['DEMO2026008']['closed'])
        c.present = False
        _, sent = tick(at('16:40'), c, state)
        self.assertEqual(sent, [])

    def test_given_up_step_does_not_poll(self):
        c = FakeClient(fail={'signin': FormChanged(ST)})
        state = {}
        tick(at('13:51'), c, state, FakeAgy())
        c.present = False   # any card read now would raise card_missing
        t, _ = tick(at('13:55'), c, state, FakeAgy())
        self.assertNotIn('card_missing', ' '.join(t.plan))


class FailClosed(unittest.TestCase):
    def test_unknown_card_value_alerts_and_stops(self):
        c = FakeClient({'signin': 'Expired'})
        t, sent = tick(at('13:51'), c)
        self.assertEqual(c.calls, [])
        self.assertTrue(any('無法辨識' in s for s in sent))
        self.assertFalse(t.state['courses']['DEMO2026008']['steps']['signin']['done'])

    def test_unknown_score_not_success(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '缺考'})
        t, sent = tick(at('15:55'), c)
        self.assertNotIn('exam_submit', c.calls)
        self.assertFalse(t.state['courses']['DEMO2026008']['steps']['exam']['done'])

    def test_blocked_prerequisite_spends_no_downstream_attempts(self):
        c = FakeClient({'signin': ST}, fail={'signout': SiteError('network_error')})
        state = {}
        tick(at('15:51'), c, state)
        steps = state['courses']['DEMO2026008']['steps']
        self.assertEqual((steps['survey']['tries'], steps['exam']['tries']), (0, 0))
        self.assertEqual(c.calls, ['signout'])


class WindowAndRetryCases(unittest.TestCase):
    def test_no_submission_after_window_closes_mid_tick(self):
        c = FakeClient()
        tick(at('14:18'), c, clock=lambda: at('14:19') + timedelta(minutes=1))   # real clock already past end
        self.assertEqual(c.calls, [])

    def test_exam_not_submitted_if_answering_ran_late(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST})
        tick(at('16:00'), c, clock=lambda: at('16:30'))   # real clock after answering: past 16:29
        self.assertNotIn('exam_submit', c.calls)

    def test_deadline_passed_to_answering(self):
        c, agy = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}), FakeAgy()
        tick(at('15:55'), c, agy=agy)
        self.assertEqual(agy.last_kw['deadline'], datetime(2026, 10, 7, 16, 29, tzinfo=TZ))

    def test_observed_low_score_counts_as_graded(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, score_after=[70])
        state = {}
        tick(at('15:55'), c, state)
        _, sent = tick(at('15:56'), c, state)
        self.assertEqual(c.calls.count('exam_submit'), 1)     # 60 (observed) + 70 = two graded
        self.assertTrue(any('手動重考' in s for s in sent))

    def test_retry_gets_previous_answers(self):
        c, agy = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}, score_after=[60, 100]), FakeAgy()
        state = {}
        tick(at('15:55'), c, state, agy)
        tick(at('15:56'), c, state, agy)
        self.assertEqual(agy.last_kw['previous'], {'score': 60, 'answers': {'1': 'A'}})

    def test_answer_failures_lead_to_one_fallback(self):
        from autopilot import answering
        c, agy = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}), FakeAgy()
        agy.fail = answering.AnswerError('gemini_failed')
        agy.fixes = c
        state = {}
        for m in ('15:55', '15:56', '15:57', '15:58'):
            tick(at(m), c, state, agy)
        self.assertEqual(agy.fallbacks, ['exam'])
        self.assertTrue(state['courses']['DEMO2026008']['steps']['exam']['done'])

    def test_first_failure_alerts_immediately(self):
        c = FakeClient(fail={'signin': SiteError('network_error')})
        _, sent = tick(at('13:51'), c)
        self.assertTrue(any('第一次失敗' in s for s in sent))

    def test_active_window_course_error_alerts(self):
        c = FakeClient(serial='999')
        _, sent = tick(at('13:51'), c)
        self.assertTrue(any('時間窗內出錯（serial_mismatch）' in s for s in sent))

    def test_ambiguous_post_with_unchanged_card_stops(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'},
                       fail={'exam_submit_lost': SiteError('post_result_unknown')})
        state = {}
        _, sent = tick(at('15:55'), c, state)
        tick(at('15:56'), c, state)
        self.assertEqual(c.calls.count('exam_submit'), 1)   # never resubmitted blindly
        self.assertTrue(any('為避免重複作答' in s for s in sent))

    def test_same_score_after_successful_post_counts(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}, score_after=[60, 60])
        state = {}
        for m in ('15:55', '15:56', '15:57'):
            _, sent = tick(at(m), c, state)
        self.assertEqual(state['courses']['DEMO2026008']['exam']['graded'], [60, 60])
        self.assertEqual(c.calls.count('exam_submit'), 2)

    def test_fallback_exam_grade_is_recorded(self):
        from autopilot import answering
        c, agy = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}), FakeAgy()
        agy.fail = answering.AnswerError('gemini_failed')
        state = {}
        def fb(course, step, secret=None, **kw):
            c.fields['exam'] = '60'
            return 'completed'
        agy.fallback = fb
        sent_all = []
        for m in ('15:55', '15:56', '15:57', '15:58'):
            _, sent = tick(at(m), c, state, agy)
            sent_all += sent
        self.assertEqual(state['courses']['DEMO2026008']['exam']['graded'], [60])
        self.assertTrue(any('代理人備援作答後成績 60' in s for s in sent_all))

    def test_no_receipt_and_unchanged_card_stops(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, score_after=[60])
        c.receipt = False            # 200 page without 提交考卷成功, card still 60
        state = {}
        _, sent = tick(at('15:55'), c, state)
        tick(at('15:56'), c, state)
        self.assertEqual(c.calls.count('exam_submit'), 1)
        self.assertEqual(state['courses']['DEMO2026008']['exam']['graded'], [60])
        self.assertTrue(any('為避免重複作答' in s for s in sent))

    def test_pending_receipt_survives_card_read_error(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, score_after=[70])
        state = {}
        real_cards, failed = c.cards, []
        def flaky():
            if 'exam_submit' in c.calls and not failed:   # the first read right after the POST fails
                failed.append(1)
                raise SiteError('network_error')
            return real_cards()
        c.cards = flaky
        tick(at('15:55'), c, state)
        self.assertTrue(state['courses']['DEMO2026008']['exam']['pending'])
        tick(at('15:56'), c, state)   # resolves from the card (70 != 60): graded, no new POST
        self.assertEqual(c.calls.count('exam_submit'), 1)
        self.assertEqual(state['courses']['DEMO2026008']['exam']['graded'], [60, 70])

    def test_missed_signin_blocks_signout(self):
        c = FakeClient()
        _, sent = tick(at('15:51'), c)   # first run ever is inside the checkout window
        self.assertEqual(c.calls, [])
        self.assertTrue(any('簽到已過時間窗' in s for s in sent))

    def test_unrelated_card_error_isolated_but_target_error_alerts(self):
        c = FakeClient()
        c.card_error = 'card_status_rows'
        _, sent = tick(at('13:51'), c)
        self.assertEqual(c.calls, [])
        self.assertTrue(any('card_status_rows' in s for s in sent))

    def test_fallback_not_started_reported_honestly(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, fail={'exam_fetch': FormChanged('x')})
        agy = FakeAgy()
        agy.fallback = lambda *a, **k: 'not_started'
        _, sent = tick(at('16:27'), c, agy=agy)
        msg = ' '.join(sent)
        self.assertIn('剩餘時間不夠', msg)
        self.assertNotIn('備援作答後成績', msg)

    def test_preflight_runs_on_first_tick_even_inside_window(self):
        c = FakeClient()
        _, sent = tick(at('13:52'), c)
        self.assertTrue(any('預檢通過' in s for s in sent))

    def test_no_handover_if_completed_just_before_fallback(self):
        c = FakeClient(fail={'signin': FormChanged('x')})
        agy = FakeAgy()
        real_fail = c._act
        def act(step, value):
            c.calls.append(step)
            c.fields[step] = ST          # the POST actually landed although the client raised
            raise FormChanged('x')
        c._act = act
        c.fields_after = None
        state = {}
        _, sent = tick(at('13:51'), c, state, agy)
        self.assertEqual(agy.fallbacks, [])
        self.assertTrue(state['courses']['DEMO2026008']['steps']['signin']['done'])

    def test_change_without_agy_completion_not_credited_to_agy(self):
        c = FakeClient(fail={'signin': FormChanged('x')})
        agy = FakeAgy()
        def fb(course, step, secret=None, **kw):
            c.fields[step] = ST
            return 'agent_failed'
        agy.fallback = fb
        _, sent = tick(at('13:51'), c, {}, agy)
        self.assertTrue(any('來源不明' in s for s in sent))
        self.assertFalse(any('完成（代理人）' in s for s in sent))

    def test_receipt_but_not_graded_keeps_pending(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}, score_after=['Pending'])
        state = {}
        tick(at('15:55'), c, state)
        tick(at('15:56'), c, state)
        self.assertEqual(c.calls.count('exam_submit'), 1)          # never resubmitted while pending
        self.assertTrue(state['courses']['DEMO2026008']['exam']['pending'])
        c.fields['exam'] = '100'
        tick(at('15:57'), c, state)
        self.assertTrue(state['courses']['DEMO2026008']['steps']['exam']['done'])

    def test_manual_retake_between_ticks_is_counted(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, score_after=[65])
        state = {}
        tick(at('15:55'), c, state)                    # observes 60, answers once -> 65
        c.fields['exam'] = '70'                          # the user retakes manually before the next tick
        _, sent = tick(at('15:57'), c, state)
        self.assertEqual(state['courses']['DEMO2026008']['exam']['graded'], [60, 65, 70])
        self.assertEqual(c.calls.count('exam_submit'), 1)   # no third automated attempt

    def test_target_card_structure_change_hands_over_once(self):
        c, agy = FakeClient(), FakeAgy()
        c.card_error = 'card_status_rows'
        state = {}
        tick(at('13:51'), c, state, agy)
        tick(at('13:52'), c, state, agy)
        self.assertEqual(agy.fallbacks, ['signin'])

    def test_fallback_marked_started_before_agy_runs(self):
        c = FakeClient(fail={'signin': FormChanged('x')})
        agy, seen = FakeAgy(), {}
        state = {}
        def fb(course, step, secret=None, on_start=None, **kw):
            on_start()
            seen['flag'] = state['courses']['DEMO2026008']['steps']['signin']['agent']
            raise SystemExit('killed mid-fallback')
        agy.fallback = fb
        with self.assertRaises(SystemExit):
            tick(at('13:51'), c, state, agy)
        self.assertTrue(seen['flag'])
        agy2 = FakeAgy()
        tick(at('13:52'), c, state, agy2)                # after the crash: never relaunch
        self.assertEqual(agy2.fallbacks, [])

    def test_recovered_state_blocks_exam_today(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST})
        state = {'recovered_at': at('09:00').isoformat()}
        tick(at('15:55'), c, state)
        self.assertNotIn('exam_submit', c.calls)

    def test_unknown_attendance_post_never_resent(self):
        c = FakeClient(fail={'signin': SiteError('post_result_unknown')})
        state = {}
        tick(at('13:51'), c, state)
        _, sent = tick(at('13:52'), c, state)
        self.assertEqual(c.calls.count('signin'), 1)
        self.assertTrue(any('為避免重複送出' in s for s in sent))

    def test_answered_but_not_updated_may_retry(self):
        c = FakeClient()
        c._act = lambda step, value: c.calls.append(step)   # platform replies 200 but card stays 待完成
        state = {}
        tick(at('13:51'), c, state)
        tick(at('13:52'), c, state)
        self.assertEqual(c.calls.count('signin'), 2)

    def test_outbox_persisted_when_queued(self):
        c = FakeClient()
        saves = []
        t = Tick({}, SECRETS, at('13:51'), client=c, agents=FakeAgy(), send=lambda text: False,
                 clock=lambda: at('13:51'), profile=PROFILE)
        t.save_now = lambda: saves.append(dict(t.state.get('outbox', {})))
        t.run([COURSE])
        self.assertTrue(any(saves))

    def test_nested_state_shape_errors_are_corrupt(self):
        for bad in ({'notices': []}, {'courses': {'DEMO2026008': 'x'}}, {'outbox': {'k': {'text': 1}}}):
            with self.assertRaises(ValueError):
                runner.check_state(bad)

    def test_end_minute_is_inclusive(self):
        c = FakeClient()
        end = datetime(2026, 10, 7, 14, 19, tzinfo=TZ)
        tick(end, c, clock=lambda: end)
        self.assertEqual(c.calls, ['signin'])

    def test_state_written_by_runner_passes_strict_schema(self):
        for fields, times in (({}, ('13:51',)), ({'signin': ST}, ('15:51', '15:52')),
                              ({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '60'}, ('15:55', '15:56'))):
            c, state = FakeClient(fields, score_after=[70, 90]), {}
            for m in times:
                tick(at(m), c, state)
            runner.check_state(json.loads(json.dumps(state)))   # must not raise

    def test_empty_pending_dict_is_corrupt(self):
        c, state = FakeClient({'signin': ST, 'signout': ST, 'survey': ST}), {}
        tick(at('15:51'), c, state)
        state = json.loads(json.dumps(state))
        state['courses']['DEMO2026008']['exam']['pending'] = {}
        with self.assertRaises(ValueError):
            runner.check_state(state)

    def test_preexisting_pass_not_credited_to_program(self):
        c = FakeClient({'signin': ST, 'signout': ST, 'survey': ST, 'exam': '100'})
        _, sent = tick(at('15:55'), c)
        self.assertTrue(any('既有成績' in s for s in sent))

    def test_redaction_covers_json_escaped_secret(self):
        tricky = {'SITE_PASSWORD': 'p"a\\ss'}.get
        blob = json.dumps({'x': 'p"a\\ss'})
        self.assertNotIn('a\\\\ss', runner.redact(blob, tricky))


class Delivery(unittest.TestCase):
    def test_undelivered_message_retried_later(self):
        c = FakeClient()
        state, sent, ok = {}, [], [False]
        def send(text):
            sent.append(text)
            return ok[0]
        Tick(state, SECRETS, at('13:51'), client=c, agents=FakeAgy(), send=send, clock=lambda: at('13:51'), profile=PROFILE).run([COURSE])
        self.assertTrue(state['outbox'])
        n = len(sent)
        Tick(state, SECRETS, at('13:55'), client=c, agents=FakeAgy(), send=send, clock=lambda: at('13:55'), profile=PROFILE).run([COURSE])
        self.assertEqual(len(sent), n)          # within resend interval
        ok[0] = True
        Tick(state, SECRETS, at('14:05'), client=c, agents=FakeAgy(), send=send, clock=lambda: at('14:05'), profile=PROFILE).run([COURSE])
        self.assertEqual(state['outbox'], {})

    def test_missed_day_still_alerts(self):
        c = FakeClient(present=False)
        state = {'installed_at': '2026-10-01T00:00:00+02:00'}
        _, sent = tick(datetime(2026, 10, 8, 9, 0, tzinfo=TZ), c, state)
        self.assertTrue(any('無法讀學員卡確認' in s for s in sent))
        self.assertTrue(state['courses']['DEMO2026008']['closed'])


class Selection(unittest.TestCase):
    def test_non_object_course_entry_reported(self):
        courses, problems = select_courses({'courses': {'DEMO2026008': 'registered'}}, at('09:00'))
        self.assertEqual((courses, problems[0][0]), ([], 'registrations_invalid:DEMO2026008'))
        _, problems = select_courses({'courses': {'DEMO2026008': {'status': 5, **DETAIL}}}, at('09:00'))
        self.assertEqual(problems[0][0], 'registrations_invalid:DEMO2026008')

    def test_not_yet_registered_course_without_status_is_silent(self):
        # real the registration robot shape for a waiting course: only 'detail'
        courses, problems = select_courses({'courses': {'DEMO2026008': {**DETAIL}}}, at('09:00'))
        self.assertEqual((courses, problems), ([], []))

    def test_missing_mode_reported_not_skipped(self):
        detail = {k: v for k, v in DETAIL.items() if k != 'mode'}
        courses, problems = select_courses({'courses': {'DEMO2026008': {'status': 'registered', 'serial': '123',
                                                                       **detail}}}, at('09:00'))
        self.assertEqual((courses, problems[0][0]), ([], 'registrations_invalid:DEMO2026008'))

    def test_invalid_registered_course_reported(self):
        autoreg = {'courses': {'DEMO2026008': {'status': 'registered', 'serial': '12!', **DETAIL}}}
        courses, problems = select_courses(autoreg, at('09:00'))
        self.assertEqual(courses, [])
        self.assertEqual(problems[0][0], 'registrations_invalid:DEMO2026008')

    def test_only_registered_online_courses_today(self):
        autoreg = {'courses': {
            'DEMO2026008': {'status': 'registered', 'serial': '123', **DETAIL},
            'DEMO2026007': {'status': 'waiting', **DETAIL},
            'DEMO2026010': {'status': 'registered', 'serial': '555', **dict(DETAIL, mode='in_person')},
            'DEMO2026011': {'status': 'registered', 'serial': '556',
                           **dict(DETAIL, checkin_start='2026-10-08T13:50:00+02:00',
                                          checkin_end='2026-10-08T14:20:00+02:00',
                                          checkout_start='2026-10-08T15:50:00+02:00',
                                          checkout_end='2026-10-08T16:30:00+02:00')},
            'DEMO2026012': {'status': 'registered', 'serial': '557',
                           **dict(DETAIL, checkin_start='2026-10-01T13:50:00+02:00',
                                          checkin_end='2026-10-01T14:20:00+02:00')},
        }}
        pick = lambda: [c['cid'] for c in select_courses(autoreg, at('09:00'))[0]]
        self.assertEqual(pick(), ['DEMO2026008'])
        autoreg['courses']['DEMO2026012'].update(dict(DETAIL, checkin_start='2026-10-05T13:50:00+02:00',
                                                          checkin_end='2026-10-05T14:20:00+02:00'))
        self.assertEqual(pick(), ['DEMO2026008', 'DEMO2026012'])

if __name__ == '__main__':
    unittest.main()
