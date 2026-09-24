"""A small, dependency-free mock course platform for demos and end-to-end tests.

It imitates a generic server-rendered e-learning platform: CSRF tokens, a student
card with status rows, check-in/out forms that require the account password and the
registration serial, a survey, a multiple-choice test and a registration flow. Time comes
from an injectable clock so demos and tests can "fast-forward" through a class day.

    python -m mock_site.server --port 8765     # then open http://127.0.0.1:8765/app/account/login
"""
import argparse
import html
import secrets as pysecrets
import threading
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo('UTC')
USER, PASSWORD = 'demo@example.com', 'demo-password'
PENDING = 'Pending'
STAMP = '%Y-%m-%d %H:%M:%S'
QUESTIONS = [('1', 'Sample question one', ['A', 'B', 'C', 'D']),
             ('2', 'Sample question two', ['A', 'B', 'C', 'D']),
             ('3', 'Sample question three', ['A', 'B', 'C', 'D'])]
ANSWER_KEY = {'1': 'A', '2': 'A', '3': 'A'}


class Platform:
    def __init__(self, clock=None, day=None):
        self.clock = clock or (lambda: datetime.now(TZ))
        day = day or self.clock().date()
        at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)   # noqa: E731
        self.courses = {'DEMO101': {
            'title': 'Demo Course 101', 'mode': 'online', 'fee': 0,
            'reg_start': at(0, 0) - timedelta(days=7), 'reg_end': at(23, 59),
            'checkin': (at(13, 50), at(14, 20)), 'checkout': (at(15, 50), at(16, 30))}}
        self.registrations = {}            # cid -> serial
        self.progress = {}                 # cid -> {signin, signout, survey, exam}
        self.sessions, self.tokens = set(), set()
        self.lock = threading.Lock()
        self.log = []                      # (method, path) of every mutating request

    def token(self):
        t = pysecrets.token_hex(8)
        self.tokens.add(t)
        return t

    def open(self, window):
        return window[0] <= self.clock() <= window[1]


def page(title, body):
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>'
            f'<body><a href="/app/account/logout">Log out</a><h1>{html.escape(title)}</h1>{body}</body></html>')


def make_handler(platform):
    P = platform

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---- helpers ----
        def send(self, code, body='', headers=None):
            data = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, to, cookie=None):
            headers = {'Location': to}
            if cookie:
                headers['Set-Cookie'] = cookie
            self.send(302, '', headers)

        def authed(self):
            c = SimpleCookie(self.headers.get('Cookie', ''))
            return 'sid' in c and c['sid'].value in P.sessions

        def form(self):
            n = int(self.headers.get('Content-Length', 0))
            return {k: v for k, v in parse_qs(self.rfile.read(n).decode(), keep_blank_values=True).items()}

        def one(self, f, key):
            v = f.get(key, [])
            return v[0] if len(v) == 1 else None

        def csrf_ok(self, f):
            t = self.one(f, 'csrf_token')
            if t in P.tokens:
                P.tokens.discard(t)
                return True
            return False

        # ---- GET ----
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            cid = q.get('course_id', '')
            if u.path == '/app/account/login':
                return self.send(200, page('Login', f'<form action="/app/account/login" method="post">'
                                           f'<input type="hidden" name="csrf_token" value="{P.token()}">'
                                           '<input type="email" name="user"><input type="password" name="password">'
                                           '<button type="submit">Log in</button></form>'))
            if u.path == '/app/courses':
                rows = ''.join(
                    f'<tr><td class="cid">{c}</td><td class="title">{html.escape(d["title"])}</td><td class="mode">{d["mode"]}</td>'
                    f'<td class="fee">{d["fee"]}</td><td class="reg">{d["reg_start"].isoformat()}|{d["reg_end"].isoformat()}</td>'
                    f'<td class="checkin">{d["checkin"][0].isoformat()}|{d["checkin"][1].isoformat()}</td>'
                    f'<td class="checkout">{d["checkout"][0].isoformat()}|{d["checkout"][1].isoformat()}</td></tr>'
                    for c, d in P.courses.items())
                return self.send(200, page('Courses', f'<table class="courses">{rows}</table>'))
            if not self.authed():
                return self.redirect('/app/account/login')
            if u.path == '/app/student/card':
                return self.send(200, page('Student card', self.cards()))
            if u.path == '/app/student/registrations':
                rows = ''.join(f'<tr><td class="cid">{c}</td><td class="serial">{s}</td><td class="status">registered</td></tr>'
                               for c, s in P.registrations.items())
                return self.send(200, page('Registrations', f'<table class="registrations">{rows}</table>'))
            if u.path == '/app/student/register-form' and cid in P.courses:
                return self.send(200, page('Register', f'<form action="/app/student/register" method="post">'
                                           f'<input type="hidden" name="csrf_token" value="{P.token()}">'
                                           f'<input type="hidden" name="course_id" value="{cid}">'
                                           '<button type="submit">Register</button></form>'))
            if u.path in ('/app/student/signin-form', '/app/student/signout-form') and cid in P.registrations:
                action = 'signin' if 'signin' in u.path else 'signout'
                return self.send(200, page(action, f'<form action="/app/student/{action}" method="post">'
                                           f'<input type="hidden" name="csrf_token" value="{P.token()}">'
                                           f'<input type="hidden" name="course_id" value="{cid}">'
                                           '<input type="email" name="user"><input type="password" name="password">'
                                           '<input type="text" name="reg_id"><button type="submit">Submit</button></form>'))
            if u.path == '/app/form/survey' and cid in P.registrations:
                radios = ''.join(f'<p>Q{i}' + ''.join(f'<input type="radio" name="q{i}" value="{v}">' for v in '12345') + '</p>'
                                 for i in (1, 2, 3))
                return self.send(200, page('Survey', f'<form action="/app/form/survey?course_id={cid}" method="post">'
                                           f'<input type="hidden" name="csrf_token" value="{P.token()}">'
                                           '<input type="hidden" name="form_id" value="7">'
                                           f'<input type="hidden" name="course_echo" value="{cid}">'
                                           f'<input type="hidden" name="course_id" value="{cid}">{radios}'
                                           '<textarea name="comments"></textarea><button type="submit">Submit</button></form>'))
            if u.path == '/app/test/exam' and cid in P.registrations:
                rows = ''.join(f'<tr><td>{n}</td><td>{html.escape(t)}</td></tr><tr><td>Answer</td><td><select name="{n}">'
                               '<option value="">choose</option>'
                               + ''.join(f'<option value="{o}">({o}) option {o}</option>' for o in opts)
                               + '</select></td></tr>' for n, t, opts in QUESTIONS)
                return self.send(200, page('Test', f'<form action="/app/test/exam?course_id={cid}" method="post">'
                                           f'<input type="hidden" name="csrf_token" value="{P.token()}">'
                                           '<input type="hidden" name="form_id" value="51">'
                                           f'<input type="hidden" name="course_id" value="{cid}"><table>{rows}</table>'
                                           '<button type="submit">Submit test</button></form>'))
            if u.path == '/app/account/logout':
                return self.redirect('/app/account/login')
            return self.send(404, page('Not found', ''))

        def cards(self):
            out = []
            for cid, serial in P.registrations.items():
                c, pr = P.courses[cid], P.progress.setdefault(cid, {})
                rows = ''.join(f'<tr><th>{label}</th><td>{html.escape(str(pr.get(key, PENDING)))}</td></tr>'
                               for key, label in (('signin', 'Check-in'), ('signout', 'Check-out'),
                                                  ('survey', 'Survey'), ('exam', 'Test score')))
                out.append(f'<div class="card"><h5>{html.escape(c["title"])}</h5><div class="serial">{serial}</div>'
                           f'<table>{rows}</table><a href="/app/course/info?course_id={cid}">Course info</a></div>')
            return ''.join(out) or '<p>No registered course.</p>'

        # ---- POST ----
        def do_POST(self):
            u = urlparse(self.path)
            f = self.form()
            with P.lock:
                P.log.append(('POST', self.path))
                if u.path == '/app/account/login':
                    if self.csrf_ok(f) and self.one(f, 'user') == USER and self.one(f, 'password') == PASSWORD:
                        sid = pysecrets.token_hex(8)
                        P.sessions.add(sid)
                        return self.redirect('/app/student/card', cookie=f'sid={sid}; Path=/; HttpOnly')
                    return self.redirect('/app/account/login')
                if not self.authed() or not self.csrf_ok(f):
                    return self.send(403, page('Forbidden', ''))
                cid = self.one(f, 'course_id')
                now = P.clock()
                stamp = now.strftime(STAMP)
                if u.path == '/app/student/register' and cid in P.courses:
                    c = P.courses[cid]
                    if c['reg_start'] <= now <= c['reg_end'] and cid not in P.registrations:
                        P.registrations[cid] = f'{100 + len(P.registrations)}'
                    return self.redirect('/app/student/registrations')
                if cid not in P.registrations:
                    return self.send(403, page('Forbidden', ''))
                pr = P.progress.setdefault(cid, {})
                if u.path in ('/app/student/signin', '/app/student/signout'):
                    kind = 'signin' if u.path.endswith('signin') else 'signout'
                    window = P.courses[cid]['checkin' if kind == 'signin' else 'checkout']
                    ok = (self.one(f, 'user') == USER and self.one(f, 'password') == PASSWORD
                          and self.one(f, 'reg_id') == P.registrations[cid] and P.open(window)
                          and (kind == 'signin' or 'signin' in pr))
                    if ok and kind not in pr:
                        pr[kind] = stamp
                    return self.redirect('/app/student/card')
                if u.path == '/app/form/survey' and 'signout' in pr and self.one(f, 'course_echo') == cid:
                    pr.setdefault('survey', stamp)
                    return self.redirect('/app/student/card')
                if u.path == '/app/test/exam' and 'survey' in pr and P.open(P.courses[cid]['checkout']):
                    correct = sum(self.one(f, n) == a for n, a in ANSWER_KEY.items())
                    pr['exam'] = str(round(100 * correct / len(ANSWER_KEY)))
                    return self.send(200, page('Result', '<script>alert("Test submitted successfully")</script>'
                                               'Test submitted successfully'))
                return self.send(409, page('Not allowed now', ''))

    return Handler


def serve(platform, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(platform))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), make_handler(Platform()))
    print(f'mock platform on http://127.0.0.1:{a.port}/app/  (user {USER} / {PASSWORD})')
    srv.serve_forever()
