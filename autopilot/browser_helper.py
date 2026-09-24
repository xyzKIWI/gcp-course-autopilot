#!/usr/bin/env python3
"""Credential-free browser tool for the fallback agent (needs Playwright + Chromium).

The agent never needs the password: it names selectors and uses the placeholders
$SITE_USER / $SITE_PASSWORD / $REGID; this helper substitutes the real values, drives a
headless Chromium session that it logged in itself, accepts dialogs, and prints only
redacted page text. Each run is locked to one course, serial, step and deadline.

  browser_helper.py --config CFG inspect --cid C --serial S --step STEP --deadline ISO [--url URL]
  browser_helper.py --config CFG act     --cid C --serial S --step STEP --deadline ISO --plan plan.json

Allowed page URLs (exact, per step): the student card for every step; the step's own
form page (check-in/out form, survey or test for this course) and its POST target.
plan.json = {"url": "<allowed url>", "steps": [
    {"op": "fill", "selector": "input[name=user]", "value": "$SITE_USER"},
    {"op": "select", "selector": "select[name='1']", "value": "D"},
    {"op": "check", "selector": "input[name='q1'][value='5']"},
    {"op": "click", "selector": "button[type=submit]"},
    {"op": "wait", "ms": 2000}]}

Network guard (Playwright route, installed before the credentials are typed): the login
phase only reaches the exact login page; afterwards only the step's pages, same-site static
assets and ONE form POST are allowed, and that POST must carry this course (and serial, or the
served csrf token + form id) exactly once. The target step must still be verifiably to do.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autopilot.config import load_config, load_secrets, redact   # noqa: E402

FORBIDDEN_TEXT = ('abandon', 'give up', 'cancel', 'register', 'delete', 'logout', 'log out',
                  '放棄', '取消', '報名', '刪除', '登出')
MAX_CLICKS = 5
BLOCKED = []


def refuse(reason):
    print(json.dumps({'refused': reason, 'blocked': BLOCKED}, ensure_ascii=False))
    sys.exit(2)


def classify(profile, rows, step):
    """'todo' only when the step is verifiably still to do; otherwise the refusal reason."""
    if rows is None:
        return 'target card not found'
    labels = [label for label, _ in rows]
    wanted = profile.labels
    if any(labels.count(l) != 1 for l in wanted.values()) or any(not v for l, v in rows if l in wanted.values()):
        return 'target card status rows unreadable'
    value = dict(rows)[wanted[step]]
    if value == profile.pending:
        return 'todo'
    if step == 'exam' and re.fullmatch(r'\d{1,3}', value):
        return 'exam already passed' if int(value) >= PASS_SCORE[0] else 'todo'
    return 'step already completed or in an unknown state'


PASS_SCORE = [80]


class Guard:
    def __init__(self, profile, args):
        self.p, self.args = profile, args
        if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', args.cid) or not re.fullmatch(r'[A-Za-z0-9]{1,12}', args.serial):
            refuse('bad cid or serial')
        if args.step not in ('signin', 'signout', 'survey', 'exam'):
            refuse('bad step')
        try:
            self.deadline = datetime.fromisoformat(args.deadline)
        except ValueError:
            refuse('bad deadline')
        q = f'?{profile.course_param}={args.cid}'
        self.card = profile.url(profile.card_path)
        self.urls = {self.card}
        if args.step in ('signin', 'signout'):
            form, action = ((profile.signin_form, profile.signin_action) if args.step == 'signin'
                            else (profile.signout_form, profile.signout_action))
            self.urls |= {profile.url(form) + q, profile.url(action)}
            self.post_url = profile.url(action)
        else:
            path = profile.survey_path if args.step == 'survey' else profile.exam_path
            self.post_url = profile.url(path) + q
            self.urls.add(self.post_url)
        self.hidden, self.posts = {}, 0
        origin = urlparse(profile.base)
        self.origin = (origin.scheme, origin.netloc)

    def time(self):
        if (self.deadline - datetime.now(self.deadline.tzinfo)).total_seconds() < 20:
            refuse('deadline passed')

    def url(self, url):
        if url.split('#')[0] not in self.urls:
            refuse(f'url not allowed for this step: {url}')

    @staticmethod
    def once(pairs, key):
        values = [v for k, v in pairs if k == key]
        return values[0] if len(values) == 1 else None

    def submission_ok(self, method, url, body, content_type):
        p, a = self.p, self.args
        if method != 'POST' or not (content_type or '').startswith('application/x-www-form-urlencoded'):
            return False
        pairs = parse_qsl(body or '', keep_blank_values=True)
        keys = [k.lower() for k, _ in pairs]
        protected = [p.course_field, p.serial_field, p.form_id_field, p.token_field, p.survey_echo_field]
        if any(keys.count(k.lower()) > 1 for k in protected):
            return False
        if url != self.post_url or self.once(pairs, p.course_field) != a.cid:
            return False
        if a.step in ('signin', 'signout'):
            return self.once(pairs, p.serial_field) == a.serial
        same_form = bool(self.hidden.get(p.form_id_field)) \
            and self.once(pairs, p.form_id_field) == self.hidden.get(p.form_id_field) \
            and self.once(pairs, p.token_field) == self.hidden.get(p.token_field)
        if a.step == 'survey':
            return same_form and self.once(pairs, p.survey_echo_field) == a.cid
        return same_form

    def read_ok(self, url, kind):
        u = urlparse(url)
        if (u.scheme, u.netloc) != self.origin:
            return False
        if kind in ('document', 'xhr', 'fetch'):
            return url.split('#')[0] in self.urls
        return kind in ('script', 'stylesheet', 'image', 'font')

    def install(self, page):
        def handle(route, request):
            url, kind = request.url.split('#')[0], request.resource_type
            if request.method != 'GET':
                if self.posts >= 1 or not self.submission_ok(request.method, url, request.post_data,
                                                             request.headers.get('content-type', '')):
                    BLOCKED.append(f'{request.method} {urlparse(url).path}')
                    return route.abort()
                self.time()
                self.posts += 1
                return route.continue_()
            if not self.read_ok(url, kind):
                BLOCKED.append(f'GET {kind} {urlparse(url).path}')
                return route.abort()
            return route.continue_()
        page.route('**/*', handle)

    def card_rows(self, page):
        return page.evaluate("""([cid, infoPath, param]) => {
            const card = [...document.querySelectorAll('div.card')].find(c => [...c.querySelectorAll('a[href]')].some(a => {
                const u = new URL(a.href);
                return u.origin + u.pathname === infoPath && u.searchParams.getAll(param).length === 1
                  && u.searchParams.get(param) === cid; }));
            if (!card) return null;
            return [...card.querySelectorAll('tr')].filter(r => r.querySelector('th') && r.querySelector('td'))
              .map(r => [r.querySelector('th').innerText.trim(), r.querySelector('td').innerText.trim()]); }""",
            [self.args.cid, self.p.url(self.p.course_info_path), self.p.course_param])

    def still_needed(self, page):
        verdict = classify(self.p, self.card_rows(page), self.args.step)
        if verdict != 'todo':
            refuse(verdict)

    def learn_form(self, page):
        self.hidden = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
            'form input[type=hidden][name]')].map(e => [e.name, e.value]))""")


def page_report(page, s):
    fields = page.eval_on_selector_all(
        'form input, form select, form textarea, form button, button, a',
        """els => els.map(e => ({tag: e.tagName.toLowerCase(), type: e.type || null, name: e.name || null,
                              text: (e.innerText || '').trim().slice(0, 40)}))""")
    body = page.inner_text('body')[:20000]
    return redact(json.dumps({'url': page.url, 'blocked': BLOCKED, 'fields': fields, 'text': body},
                             ensure_ascii=False, indent=1), s)


def open_session(p, profile, s, guard):
    browser = p.chromium.launch(headless=True)
    page = browser.new_context().new_page()
    page.on('dialog', lambda d: d.accept())
    login = profile.url(profile.login_path)

    def login_phase(route, request):
        url = request.url.split('#')[0]
        if (urlparse(url).scheme, urlparse(url).netloc) != guard.origin:
            return route.abort()
        if request.resource_type in ('document', 'xhr', 'fetch') and url not in (login, guard.card):
            return route.abort()
        return route.continue_()
    page.route('**/*', login_phase)
    page.goto(login)
    if page.url.split('#')[0] != login:
        refuse('login page not at the expected address')
    page.fill(f'input[name={profile.user_field}]', s('SITE_USER'))
    page.fill(f'input[name={profile.password_field}]', s('SITE_PASSWORD'))
    page.click('form button[type=submit]')
    page.wait_for_load_state('networkidle')
    if page.locator(f'input[name={profile.password_field}]').count():
        refuse('login failed')
    page.unroute('**/*', login_phase)
    guard.install(page)
    page.goto(guard.card)
    page.wait_for_load_state('networkidle')
    rows = guard.card_rows(page)
    if rows is None:
        refuse('need exactly one target card')
    serials = page.evaluate("""([cid, infoPath, param, sel]) => [...document.querySelectorAll('div.card')].filter(c =>
        [...c.querySelectorAll('a[href]')].some(a => { const u = new URL(a.href);
          return u.origin + u.pathname === infoPath && u.searchParams.get(param) === cid; }))
        .map(c => (c.querySelector(sel)?.innerText || '').trim())""",
        [guard.args.cid, profile.url(profile.course_info_path), profile.course_param, profile.serial_selector])
    if serials != [guard.args.serial]:
        refuse('need exactly one target card with this serial')
    guard.still_needed(page)
    return browser, page


def value_for(raw, s, serial):
    if raw in ('$SITE_USER', '$SITE_PASSWORD', '$REGID'):
        return {'$SITE_USER': s('SITE_USER'), '$SITE_PASSWORD': s('SITE_PASSWORD'), '$REGID': serial}[raw]
    if not isinstance(raw, str) or len(raw) > 40:
        refuse('literal values must be short strings')
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('cmd', choices=['inspect', 'act'])
    for flag in ('--cid', '--serial', '--step', '--deadline'):
        ap.add_argument(flag, required=True)
    ap.add_argument('--url')
    ap.add_argument('--plan')
    args = ap.parse_args()
    cfg = load_config(args.config)
    profile = cfg['profile']
    PASS_SCORE[0] = cfg.get('policy', {}).get('pass_score', 80)
    guard = Guard(profile, args)
    guard.time()
    s = load_secrets(cfg['paths']['secrets_file'])
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = open_session(p, profile, s, guard)
        try:
            if args.cmd == 'inspect':
                url = args.url or guard.card
                guard.url(url)
                page.goto(url)
                page.wait_for_load_state('networkidle')
                print(page_report(page, s))
                return 0
            plan = json.loads(Path(args.plan).read_text())
            guard.url(plan['url'])
            page.goto(plan['url'])
            page.wait_for_load_state('networkidle')
            if args.step in ('survey', 'exam'):
                guard.learn_form(page)
            clicks = 0
            for step in plan['steps']:
                guard.time()
                op = step.get('op')
                if op == 'fill':
                    page.fill(step['selector'], value_for(step['value'], s, args.serial))
                elif op == 'select':
                    page.select_option(step['selector'], value_for(step['value'], s, args.serial))
                elif op == 'check':
                    page.check(step['selector'])
                elif op == 'click':
                    clicks += 1
                    if clicks > MAX_CLICKS:
                        refuse('too many clicks')
                    label = page.locator(step['selector']).first.evaluate(
                        """e => { const t = e.closest('button, a, input[type=submit], input[type=button]') || e;
                                  return (t.innerText || '') + ' ' + (t.value || '') + ' ' + (t.getAttribute('href') || ''); }""")
                    if any(word in label.lower() for word in FORBIDDEN_TEXT):
                        refuse('forbidden button')
                    page.click(step['selector'])
                    page.wait_for_load_state('networkidle')
                    guard.url(page.url)
                elif op == 'wait':
                    page.wait_for_timeout(min(int(step['ms']), 10000))
                else:
                    refuse(f'unknown op {op}')
            page.goto(guard.card)
            page.wait_for_load_state('networkidle')
            print(page_report(page, s))
            return 0
        finally:
            browser.close()


if __name__ == '__main__':
    sys.exit(main())
