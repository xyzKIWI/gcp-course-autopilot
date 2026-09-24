"""One scheduler tick (run it every 60 s from launchd/cron/systemd).

Reads registered courses from the registration state (read-only). On a class day it
checks in, checks out, fills the survey and takes the course test inside the platform's
windows, verifying each step on the student card. The real clock is re-checked right
before every submission. Failures alert on first occurrence, fall back to an AI agent
once, and always end in a notification (retried until delivered). --dry-run logs in and
reads the card but never posts a form, calls a model/agent, notifies or writes anything.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import answering, fallback
from .config import load_config, load_secrets, redact
from .notify import make_notifier
from .site import STEPS, Client, FormChanged, SiteError, done, score

LABEL = {'signin': '簽到', 'signout': '簽退', 'survey': '滿意度', 'exam': '課後測驗'}


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Agents:
    """Answering + fallback, configured in config.toml (no model or vendor is assumed)."""

    def __init__(self, cfg, config_path, clock):
        self.cfg, self.config_path, self.clock = cfg, config_path, clock

    def answer(self, course, paper, material, secret=None, deadline=None, previous=None):
        p = self.cfg['profile']
        hosts = (urlparse(p.base).hostname, *p.material_hosts)
        return answering.answer(self.cfg['answer']['providers'], course['title'], paper, material, secret,
                                deadline, clock=self.clock, previous=previous, allowed_hosts=hosts)

    def fallback(self, course, step, secret=None, deadline=None, on_start=None):
        return fallback.hand_over(self.cfg.get('fallback'), course, step, deadline, self.clock,
                                  self.config_path, on_start=on_start)


class Tick:
    def __init__(self, state, secret, now, dry_run=False, client=None, agents=None, send=None, clock=None,
                 profile=None, policy=None):
        self.p = profile
        policy = policy or {}
        self.pass_score = policy.get('pass_score', 80)
        self.max_tries = policy.get('max_tries', 3)
        self.max_graded = policy.get('max_graded', 2)
        self.preflight_at = time.fromisoformat(policy.get('preflight_at', '08:30'))
        self.margin = timedelta(minutes=policy.get('margin_minutes', 1))
        self.resend_after = timedelta(minutes=policy.get('resend_minutes', 10))
        self.state = state
        self.secret = secret
        self.now = now
        self.dry = dry_run
        self._client = client
        self.agents = agents
        self.send = send
        self.clock = clock or (lambda: datetime.now(self.p.tz))   # real time, re-read before every submission
        self.save_now = lambda: None   # main() points this at the state file (persist before risky steps)
        self.plan = []   # human-readable actions (dry-run output and log)

    # ---- helpers -------------------------------------------------------
    def client(self):
        if self._client is None:
            self._client = Client(self.p, self.secret, dry_run=self.dry, clock=self.clock).login()
        return self._client

    def code(self, exc):
        return redact(exc, self.secret)[:120]

    def say(self, key, text):
        """Queue a Telegram message once per (key, text); retried until delivered."""
        text = redact(text, self.secret)
        self.plan.append(text)
        if self.dry:
            return
        digest = hashlib.sha256(text.encode()).hexdigest()
        notices = self.state.setdefault('notices', {})
        if notices.get(key) == digest:
            return
        notices[key] = digest
        self.state.setdefault('outbox', {})[key] = {'text': text, 'last_try': None}
        self.save_now()   # queued durably before any delivery attempt
        self.flush(only=key)

    def flush(self, only=None):
        outbox = self.state.get('outbox') or {}
        for key in list(outbox):
            if only and key != only:
                continue
            item = outbox[key]
            if item['last_try'] and self.now - datetime.fromisoformat(item['last_try']) < self.resend_after:
                continue
            item['last_try'] = self.now.isoformat()
            if self.send(item['text']):
                del outbox[key]
                self.state.setdefault('notification_results', {})[key] = {'ok': True, 'at': self.now.isoformat()}
            self.save_now()

    def card(self, course):
        card = self.client().cards().get(course['cid'])
        if card is None:
            raise SiteError('card_missing')
        if card.get('error'):
            raise FormChanged(card['error'])
        if card['serial'] != course['serial']:
            raise SiteError('serial_mismatch')
        return card

    def still_open(self, end, need=0):
        """True if the real clock leaves at least `need` seconds before the window end."""
        return (end - self.clock()).total_seconds() >= need   # the end minute itself is inside the window

    def unknown(self, course, rec, step, value):
        rec['steps'][step]['gave_up'] = True
        self.say(f"{course['cid']}:{step}:unknown",
                 f"❌ 課程 {course['cid']} {LABEL[step]}：學員卡出現無法辨識的狀態「{value}」，程式停手，請手動確認。")
        return False

    def first_failure(self, course, step, error, end):
        self.say(f"{course['cid']}:{step}:retrying",
                 f"⚠️ 課程 {course['cid']} {LABEL[step]}第一次失敗（{error}），窗內持續自動重試（到 {end:%H:%M}）。"
                 '若你方便，也可以直接手動處理。')

    # ---- scheduling ----------------------------------------------------
    def windows(self, course):
        d = course
        p = datetime.fromisoformat
        return {
            'signin': (p(d['checkin_start']) + self.margin, p(d['checkin_end']) - self.margin),
            'signout': (p(d['checkout_start']) + self.margin, p(d['checkout_end']) - self.margin),
        }

    def window_of(self, course, step):
        return self.windows(course)['signin' if step == 'signin' else 'signout']

    def run(self, courses):
        installed = datetime.fromisoformat(self.state.setdefault('installed_at', self.now.isoformat()))
        for course in courses:
            rec = self.state.setdefault('courses', {}).setdefault(course['cid'], {
                'preflight': None, 'closed': None,
                'steps': {s: {'done': None, 'tries': 0, 'agent': False, 'error': None, 'expired': False} for s in STEPS},
                'exam': {'graded': [], 'failures': 0, 'engine': None, 'uncertain': [], 'pending': None, 'last_seen': None,
                         'answers': None}})
            if rec['closed']:
                continue
            recovered = self.state.get('recovered_at')
            if recovered and datetime.fromisoformat(recovered).date() == self.now.date() \
                    and not rec['steps']['exam'].get('gave_up'):
                rec['steps']['exam']['gave_up'] = True   # possible lost pending receipt: no automatic exam today
            win = self.windows(course)
            last_end = win['signout'][1] + self.margin
            if last_end < installed:
                rec['closed'] = 'before_install'   # nothing this robot was responsible for
                continue
            try:
                self.course(course, rec)
            except SiteError as exc:
                code = self.code(exc)
                self.plan.append(f"{course['cid']}: {code}")
                self.state['last_error'] = code
                active = next((s for s, (a, b) in (('簽到', win['signin']), ('簽退', win['signout']))
                               if a <= self.now <= b), None)
                if active:
                    self.say(f"{course['cid']}:active_error:{code}",
                             f"⚠️ 課程 {course['cid']} {active}時間窗內出錯（{code}），持續自動重試中；"
                             '若你方便，也可以直接手動處理。')
                elif self.now > last_end:
                    pending = [LABEL[s] for s in STEPS if not rec['steps'][s]['done']]
                    self.say(f"{course['cid']}:closed_unverified",
                             f"❌ 課程 {course['cid']} 已過簽退窗，無法讀學員卡確認（{code}）；"
                             f"程式紀錄未完成：{'、'.join(pending) or '無'}。請到修課紀錄手動確認。")
            if self.now > last_end and not self.dry:
                rec['closed'] = self.now.isoformat()
        if not self.dry:
            self.flush()
        return self.plan

    def course(self, course, rec):
        cid, title = course['cid'], course['title']
        win = self.windows(course)
        if rec['preflight'] is None and self.now.time() >= self.preflight_at \
                and self.now.date() == win['signin'][0].date() and self.now <= win['signout'][1]:
            self.preflight(course, rec)   # first tick of the class day at/after 08:30, even inside a window
        for step in STEPS:
            state = rec['steps'][step]
            if state['done']:
                continue
            start, end = self.window_of(course, step)
            if self.now < start:
                return   # later steps cannot run before this one
            if self.now > end:
                if not state['expired']:
                    self.close_step(course, rec, step, title)
                if not state['done']:
                    return   # an unfinished prerequisite blocks every later step, even after its window
                continue
            if not self.step(course, rec, step):
                return   # later steps depend on this one: never spend their attempts

    def close_step(self, course, rec, step, title):
        """Window over: one last card read, then either mark done or alert once."""
        cid = course['cid']
        value = self.card(course)['fields'][step]
        try:
            ok = (score(self.p, value) or 0) >= self.pass_score if step == 'exam' else done(self.p, value)
        except FormChanged:
            ok = False
        if ok:
            self.mark(course, rec, step, value)
            return
        rec['steps'][step]['expired'] = True
        self.say(f'{cid}:{step}:expired', f"❌ 課程 {cid}「{title}」{LABEL[step]}已過時間窗仍未完成（學員卡：{value}），請手動確認。")

    def preflight(self, course, rec):
        cid = course['cid']
        rec['preflight'] = self.now.isoformat()
        try:
            self.card(course)
            self.say(f'{cid}:preflight', f"✅ 今天 課程 {cid} 預檢通過：登入正常、學員卡有此課、序號 {course['serial']}。"
                                         f"簽到 {course['checkin']}、簽退 {course['checkout']} 會自動處理。")
        except SiteError as exc:
            self.say(f'{cid}:preflight', f"⚠️ 今天 課程 {cid} 預檢異常：{self.code(exc)}。窗內仍會自動重試，請留意。")

    # ---- signin / signout / survey ------------------------------------
    def step(self, course, rec, step):
        cid = course['cid']
        state = rec['steps'][step]
        if state.get('gave_up'):
            return False   # already alerted; the window-end check re-reads the card once
        _, end = self.window_of(course, step)
        try:
            card = self.card(course)
        except FormChanged as exc:   # the target card itself changed shape: hand over (once)
            if self.dry:
                self.plan.append(f'{cid}: target card changed ({exc}); would hand over to the agent')
                return False
            state['error'] = self.code(exc)
            self.first_failure(course, step, state['error'], end)
            return self.fallback(course, rec, step, end)
        if step == 'exam':
            return self.exam(course, rec, card)
        value = card['fields'][step]
        try:
            if done(self.p, value):
                return self.mark(course, rec, step, value)
        except FormChanged:
            return self.unknown(course, rec, step, value)
        if self.dry:
            self.plan.append(f'{cid}: would {step}')
            return False
        if state.get('pending') == 'unknown':
            # The last POST may have reached the platform but was never confirmed: never resend blindly.
            state['gave_up'] = True
            self.say(f'{cid}:{step}:ambiguous', f"❌ 課程 {cid} {LABEL[step]}：上次送出結果不明且學員卡仍未更新，"
                                                f"為避免重複送出，程式停手；請在 {end:%H:%M} 前手動確認。")
            return False
        state['pending'] = None   # 'answered' (platform replied) is safe to retry
        changed = False
        if state['tries'] < self.max_tries:
            if not self.still_open(end):
                return False   # the window closed while this tick was running
            state['tries'] += 1
            state['pending'] = 'unknown'
            self.save_now()   # durable before the POST can leave this machine
            try:
                client = self.client()
                if step == 'survey':
                    client.survey(cid, end)
                else:
                    client.attendance(step, cid, course['serial'], end)
                state['pending'] = 'answered'   # the platform replied (HTTP 200); card decides success
            except FormChanged as exc:
                changed, state['error'], state['pending'] = True, self.code(exc), None   # rejected before sending
            except SiteError as exc:
                state['error'] = self.code(exc)
                if not (exc.args and exc.args[0] in ('post_result_unknown', 'post_http_error')):
                    state['pending'] = None   # failed before the POST was sent
            self.save_now()
            value = self.card(course)['fields'][step]
            try:
                finished = done(self.p, value)
            except FormChanged:
                return self.unknown(course, rec, step, value)
            if finished or state['pending'] == 'answered':
                state['pending'] = None
            if finished:
                return self.mark(course, rec, step, value)
            if state['tries'] == 1:
                self.first_failure(course, step, state['error'] or 'card_not_updated', end)
        if (changed or state['tries'] >= self.max_tries) and not state['agent']:
            return self.fallback(course, rec, step, end)
        return False

    def peek(self, course, step):
        """Card value for `step`, or None if the target card itself is structurally broken."""
        try:
            return self.card(course)['fields'][step]
        except FormChanged:
            return None

    def fallback(self, course, rec, step, end):
        cid, state = course['cid'], rec['steps'][step]

        def finished(v):
            return v is not None and ((score(self.p, v) or 0) >= self.pass_score if step == 'exam' else done(self.p, v))

        if state['agent']:
            return False   # the single fallback was already started (possibly before a crash): never relaunch
        before = self.peek(course, step)
        try:
            if finished(before):   # completed meanwhile (late update or manual): never hand over
                return self.mark(course, rec, step, before, via='程式／手動（接手前已完成）')
        except FormChanged:
            return self.unknown(course, rec, step, before)

        def started():
            state['agent'] = True
            state['fallback_started'] = self.clock().isoformat()
            self.save_now()   # durable before the agent can act
            self.say(f'{cid}:{step}:fallback', f"🛟 課程 {cid} {LABEL[step]}程式失敗（{state['error']}），已由備援代理人接手處理。")

        outcome = self.agents.fallback(course, step, self.secret, deadline=end, on_start=started)
        state['fallback_outcome'] = outcome
        value = self.peek(course, step)
        changed = value is not None and value != before
        try:
            ok = changed and finished(value)
        except FormChanged:
            return self.unknown(course, rec, step, value)
        by_agent = outcome == 'completed' and changed
        if step == 'exam' and changed and score(self.p, value) is not None:
            rec['exam']['graded'].append(score(self.p, value))
            rec['exam']['last_seen'] = value
            rec['exam']['engine'] = '代理人備援' if by_agent else '來源不明（非代理人回報完成）'
        if ok:
            return self.mark(course, rec, step, value, via='代理人' if by_agent else '來源不明（卡片有變但代理人未回報完成）')
        state['gave_up'] = True
        shown = value if value is not None else '學員卡結構無法辨識'
        if outcome == 'not_started':
            why = f'程式失敗，且剩餘時間不夠讓代理人接手（學員卡：{shown}）'
        elif step == 'exam' and by_agent and score(self.p, value) is not None:
            why = f"代理人備援作答後成績 {score(self.p, value)} 分，未達 {self.pass_score}（備援只有一次）"
        else:
            why = f'程式與代理人都未完成（代理人：{outcome}；學員卡{"變成" if changed else "目前"}：{shown}）'
        self.say(f'{cid}:{step}:failed', f"❌ 課程 {cid} {LABEL[step]}：{why}，請立刻手動處理（窗口到 {end:%H:%M}）。")
        return False

    def mark(self, course, rec, step, value, via='程式'):
        rec['steps'][step]['done'] = self.now.isoformat()
        self.say(f"{course['cid']}:{step}:done", f"✅ 課程 {course['cid']} {LABEL[step]}完成（{via}）：{value}")
        return True

    # ---- exam ----------------------------------------------------------
    def exam(self, course, rec, card):
        """Exam state machine, reconciled from the card every tick.

        graded   = scores actually recorded by the platform (the card only shows the last one);
        pending  = a submission record persisted *before* the POST: {before, receipt};
                   while it exists nothing is resubmitted; it settles only on a numeric score:
                   card changed to a score -> new grade; unchanged score + platform receipt -> new grade;
                   receipt/changed but still 待完成 -> keep polling (window-end check alerts if it never grades);
                   unchanged + no receipt -> stop and alert (never guess, never resubmit);
        failures = answer/transport failures without a new grade.
        >=80 passed | two graded results below 80 -> alert | 3 failures -> one agent fallback.
        """
        cid, exam, state = course['cid'], rec['exam'], rec['steps']['exam']
        _, end = self.window_of(course, 'exam')
        value = card['fields']['exam']
        try:
            current = score(self.p, value)
        except FormChanged:
            return self.unknown(course, rec, 'exam', value)
        if exam.get('pending'):
            return self.resolve(course, rec, value, current, end)
        external = current is not None and value != exam.get('last_seen')
        if external:
            exam['graded'].append(current)   # a score we did not produce (pre-existing, manual retake, late grade)
        exam['last_seen'] = value
        if current is not None and current >= self.pass_score:
            return self.mark(course, rec, 'exam', f'{current} 分', via='既有成績／來源不明（非本程式送出）')
        if len(exam['graded']) >= self.max_graded:
            state['gave_up'] = True
            self.say(f'{cid}:exam:low', f"❌ 課程 {cid} 課後測驗 {exam['graded']} 分，兩次都未達 {self.pass_score}，"
                                        f"請在 {end:%H:%M} 前手動重考（只記最後一次）。")
            return False
        if exam['failures'] >= self.max_tries:
            if state['agent']:
                return False
            return self.fallback(course, rec, 'exam', end)
        if self.dry:
            self.plan.append(f'{cid}: would answer exam')
            return False
        previous = {'score': exam['graded'][-1], 'answers': exam['answers']} if exam['graded'] and exam['answers'] else None
        try:
            paper = self.client().exam(cid)
            answers, uncertain, engine = self.agents.answer(course, paper, card.get('material'), self.secret,
                                                         deadline=end, previous=previous)
            if not self.still_open(end, need=5):
                return False   # answering ran past the window: do not submit late
            exam.update(engine=engine, uncertain=uncertain, answers=answers)
        except FormChanged as exc:
            state['error'] = self.code(exc)
            return self.fallback(course, rec, 'exam', end)
        except (SiteError, answering.AnswerError) as exc:
            state['error'] = self.code(exc)
            exam['failures'] += 1
            if exam['failures'] == 1:
                self.first_failure(course, 'exam', state['error'], end)
            return False
        exam['pending'] = {'before': value, 'receipt': False, 'at': self.now.isoformat()}
        self.save_now()   # durable before the POST: a crash or read error later cannot lose it
        try:
            exam['pending']['receipt'] = bool(self.client().submit_exam(paper, answers, end))
        except SiteError as exc:
            state['error'] = self.code(exc)
            if exc.args and exc.args[0] == 'deadline_passed':
                exam['pending'] = None   # refused locally before sending: nothing reached the platform
                return False
        self.save_now()
        value = self.card(course)['fields']['exam']
        try:
            current = score(self.p, value)
        except FormChanged:
            return self.unknown(course, rec, 'exam', value)
        return self.resolve(course, rec, value, current, end)

    def resolve(self, course, rec, value, current, end):
        """Settle a pending submission from evidence only."""
        cid, exam, state = course['cid'], rec['exam'], rec['steps']['exam']
        pending = exam['pending']
        if value == pending['before'] and not pending['receipt']:
            state['gave_up'] = True
            self.say(f'{cid}:exam:ambiguous', f"❌ 課程 {cid} 課後測驗送出後沒看到平台的「提交考卷成功」，學員卡仍是「{value}」。"
                                              f"為避免重複作答，程式停手；請在 {end:%H:%M} 前手動確認／重考。")
            return False
        if current is None:
            return False   # accepted (receipt or changed) but not graded yet: keep pending, poll, never resubmit
        exam['pending'] = None
        exam['graded'].append(current)
        exam['last_seen'] = value
        note = f"（{exam['engine']} 作答）" + (f"；不確定題：{', '.join(exam['uncertain'])}" if exam['uncertain'] else '')
        if current >= self.pass_score:
            return self.mark(course, rec, 'exam', f'{current} 分{note}')
        self.say(f"{cid}:exam:graded{len(exam['graded'])}",
                 f"⚠️ 課程 {cid} 課後測驗第 {len(exam['graded'])} 次：{current} 分{note}，會自動再答一次。"
                 if len(exam['graded']) < self.max_graded else
                 f"⚠️ 課程 {cid} 課後測驗第 {len(exam['graded'])} 次：{current} 分{note}。")
        return False




# ---- registration state (read-only; written by register.py) ----------------
def validate(cid, item):
    """Return a normalized course dict or raise ValueError(reason)."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', cid):
        raise ValueError('bad_cid')
    serial = str(item.get('serial', ''))
    if not re.fullmatch(r'[A-Za-z0-9]{1,12}', serial):
        raise ValueError('bad_serial')
    times = {}
    for key in ('checkin_start', 'checkin_end', 'checkout_start', 'checkout_end'):
        value = datetime.fromisoformat(item[key])
        if value.tzinfo is None:
            raise ValueError('naive_time')
        times[key] = value
    if not (times['checkin_start'] < times['checkin_end'] <= times['checkout_start'] < times['checkout_end']):
        raise ValueError('bad_time_order')
    for key in ('title', 'checkin', 'checkout'):
        if not isinstance(item.get(key), str):
            raise ValueError('missing_' + key)
    return {'cid': cid, 'serial': serial, **{k: item[k] for k in ('title', 'checkin', 'checkout')},
            **{k: item[k] for k in times}}


def select_courses(registrations, now, lookback_days=3):
    """Registered online courses whose check-in day is today or within the lookback.

    Returns (courses, problems); problems are (key, message) pairs for notification.
    Records without 'status' are courses that are not registered yet (silently skipped).
    """
    courses, problems = [], []
    lookback = timedelta(days=lookback_days)
    for cid, item in sorted(registrations['courses'].items()):
        if not isinstance(item, dict) or ('status' in item and not isinstance(item['status'], str)):
            problems.append((f'registrations_invalid:{cid}', f'🚨 報名資料裡的 {cid} 不是正常的課程紀錄，請手動留意。'))
            continue
        if item.get('status') != 'registered':
            continue
        mode = item.get('mode')
        if mode == 'in_person':
            continue   # recognized, not handled by this robot
        try:
            if mode != 'online':
                raise ValueError('unknown_mode')
            course = validate(cid, item)
        except (KeyError, TypeError, ValueError) as exc:
            reason = exc.args[0] if isinstance(exc, ValueError) and exc.args else type(exc).__name__
            problems.append((f'registrations_invalid:{cid}',
                             f'🚨 已報名的 {cid} 資料不完整（{reason}），無法自動處理，請手動留意。'))
            continue
        start = datetime.fromisoformat(course['checkin_start'])
        if now.date() - lookback <= start.astimezone(now.tzinfo).date() <= now.date():
            courses.append(course)
    return courses, problems


def load_registrations(path):
    """Return (data, problem). Never writes the file."""
    path = Path(path)
    if not path.exists():
        return {}, ('registrations_missing', '🚨 找不到報名狀態檔，讀不到已報名課程。')
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, ('registrations_unreadable', '🚨 報名狀態檔讀取／解析失敗。')
    if not isinstance(data, dict) or not isinstance(data.get('courses'), dict):
        return {}, ('registrations_schema', '🚨 報名狀態檔結構不對（沒有 courses）。')
    return data, None


def check_state(state):
    """Raise ValueError unless every persisted structure has exactly the shape this code relies on."""
    def need(cond):
        if not cond:
            raise ValueError('state_shape')

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    def opt_str(v):
        return v is None or isinstance(v, str)

    def iso(v):
        need(isinstance(v, str))
        datetime.fromisoformat(v)

    need(isinstance(state, dict))
    for key in ('notices', 'notification_results', 'outbox', 'courses'):
        need(isinstance(state.get(key, {}), dict))
    need(all(isinstance(v, str) for v in state.get('notices', {}).values()))
    for item in state.get('outbox', {}).values():
        need(isinstance(item, dict) and set(item) == {'text', 'last_try'} and isinstance(item['text'], str))
        if item['last_try'] is not None:
            iso(item['last_try'])
    for key in ('installed_at', 'recovered_at', 'last_run'):
        if key in state:
            iso(state[key])
    need(opt_str(state.get('last_error')))
    step_keys = {'done', 'tries', 'agent', 'error', 'expired'}
    for rec in state.get('courses', {}).values():
        need(isinstance(rec, dict) and isinstance(rec.get('steps'), dict) and isinstance(rec.get('exam'), dict))
        need(opt_str(rec.get('preflight')) and opt_str(rec.get('closed')))
        need(set(rec['steps']) == set(STEPS))
        for v in rec['steps'].values():
            need(isinstance(v, dict) and step_keys <= set(v))
            need(opt_str(v['done']) and is_int(v['tries']) and isinstance(v['agent'], bool)
                 and opt_str(v['error']) and isinstance(v['expired'], bool))
            need(isinstance(v.get('gave_up', False), bool))
            need(v.get('pending') in (None, 'unknown', 'answered'))
            need(opt_str(v.get('fallback_started')) and opt_str(v.get('fallback_outcome')))
        exam = rec['exam']
        need({'graded', 'failures', 'engine', 'uncertain', 'answers', 'pending', 'last_seen'} <= set(exam))
        need(isinstance(exam['graded'], list) and all(is_int(g) for g in exam['graded']))
        need(is_int(exam['failures']) and opt_str(exam['engine']) and opt_str(exam['last_seen']))
        need(isinstance(exam['uncertain'], list) and all(isinstance(u, str) for u in exam['uncertain']))
        need(exam['answers'] is None or (isinstance(exam['answers'], dict)
                                         and all(isinstance(k, str) and isinstance(v, str) for k, v in exam['answers'].items())))
        pending = exam['pending']
        if pending is not None:
            need(isinstance(pending, dict) and set(pending) == {'before', 'receipt', 'at'}
                 and opt_str(pending['before']) and isinstance(pending['receipt'], bool))
            iso(pending['at'])


def load_state(path, readonly=False):
    """Return (state, corrupt_copy). A broken file is preserved and replaced by a fresh state that
    blocks today's exam (pending-submission evidence may have been lost). readonly never touches disk."""
    if not path.exists():
        return {}, None
    try:
        state = json.loads(path.read_text())
        check_state(state)
        return state, None
    except (OSError, ValueError):
        if readonly:
            return {'corrupt_readonly': True}, None
        copy = path.with_name(f"state.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            os.replace(path, copy)
        except OSError:
            pass
        return {'recovered_at': datetime.now().isoformat()}, copy


def tick_once(cfg, args, state, path, corrupt):
    """One tick. With --dry-run nothing is written anywhere (path is only read by the caller)."""
    tz = cfg['profile'].tz
    now = datetime.fromisoformat(args.now).astimezone(tz) if args.now else datetime.now(tz)
    if 'installed_at' not in state and not args.dry_run:
        state['installed_at'] = now.isoformat()
        save(path, state)
    registrations, problem = load_registrations(cfg['paths']['registrations'])
    courses, problems = ([], [problem]) if problem else \
        select_courses(registrations, now, cfg.get('policy', {}).get('lookback_days', 3))
    courses = [c for c in courses if not (state.get('courses', {}).get(c['cid']) or {}).get('closed')]
    notices = state.get('notices', {})
    new_problems = [(k, m) for k, m in problems
                    if notices.get(k) != hashlib.sha256(m.encode()).hexdigest()]
    if corrupt and not args.dry_run:
        problems.append(('state_corrupt', f'🚨 自動出席程式的紀錄檔損壞，已另存 {corrupt.name} 並重新開始；'
                                          '為避免重複作答，今天的課後測驗改為停手、請手動處理（簽到退與滿意度照常，依學員卡判斷）。'))
        new_problems.append(problems[-1])
    if not courses and not state.get('outbox') and not new_problems and not args.dry_run:
        return 0   # nothing due: no network at all
    secret = load_secrets(cfg['paths']['secrets_file'])
    if state.get('corrupt_readonly'):
        print('note: state.json is corrupt; a normal tick would quarantine it (dry-run leaves it untouched)')
        state = {}
    clock = (lambda: now) if args.dry_run else (lambda: datetime.now(tz))
    tick = Tick(state, secret, now, dry_run=args.dry_run, clock=clock, profile=cfg['profile'],
                policy=cfg.get('policy', {}), send=make_notifier(cfg.get('notify'), secret),
                agents=Agents(cfg, cfg['_path'], clock))
    if not args.dry_run:
        tick.save_now = lambda: save(path, state)
    try:
        for key, message in problems:
            tick.say(key, message)
        if args.dry_run:
            cards = tick.client().cards()
            tick.plan.append(f'login ok; cards on StudentCard: {sorted(cards)}')
        plan = tick.run(courses)
        code = 0
    except Exception as exc:  # record a type only; never raw text
        plan, code = tick.plan + [f'error: {type(exc).__name__}'], 1
        state['last_error'] = type(exc).__name__
        tick.say('runner_error', f'🚨 自動出席程式錯誤：{type(exc).__name__}，下一分鐘重試。')
    state['last_run'] = now.isoformat()
    if not args.dry_run:
        save(path, state)
    print(redact(json.dumps({'at': now.isoformat(), 'dry_run': args.dry_run,
                             'courses': [c['cid'] for c in courses], 'plan': plan}, ensure_ascii=False), secret))
    return code



def main(argv=None):
    p = argparse.ArgumentParser(description='course autopilot: one scheduler tick')
    p.add_argument('--config', required=True)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--status', action='store_true')
    p.add_argument('--now', help='ISO time override; only with --dry-run')
    args = p.parse_args(argv)
    if args.now and not args.dry_run:
        p.error('--now requires --dry-run')
    cfg = load_config(args.config)
    state_dir = Path(cfg['paths']['state_dir'])
    path = state_dir / 'state.json'
    if args.status or args.dry_run:
        # Strictly read-only: no directories, no lock file, no quarantine, no saves.
        state, _ = load_state(path, readonly=True)
        if args.status:
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        return tick_once(cfg, args, state, path, None)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    with (state_dir / 'runner.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state, corrupt = load_state(path)
        return tick_once(cfg, args, state, path, corrupt)


if __name__ == '__main__':
    sys.exit(main())
