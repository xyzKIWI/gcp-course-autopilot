"""HTTP client for a course platform: login, student card, check-in/out, survey, test.

Everything platform-specific (URLs, field names, labels, timestamp format) lives in
a SiteProfile, so the same client can drive the bundled mock site or any platform
with the same shape. The client is deliberately strict:

* no request follows redirects automatically; only same-origin GET redirects are
  followed by hand, and 307/308 are refused (a credential-bearing POST is never replayed);
* every form is validated against an exhaustive inventory of its named controls;
  anything unexpected raises FormChanged so the caller can fall back instead of guessing;
* the deadline is checked immediately before every mutating POST;
* card status is fail-closed: only a real timestamp is "done", only the exact pending
  text is "pending", anything else is an unknown state.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

STEPS = ('signin', 'signout', 'survey', 'exam')


@dataclass
class SiteProfile:
    base: str                                   # e.g. 'http://127.0.0.1:8765/app/'
    timezone: str = 'UTC'
    login_path: str = 'account/login'
    logout_path: str = 'account/logout'         # a link to this path proves an authenticated page
    card_path: str = 'student/card'
    course_info_path: str = 'course/info'       # card identity: <a href=".../course/info?course_id=X">
    course_param: str = 'course_id'
    signin_form: str = 'student/signin-form'    # GET returns the check-in form for ?course_id=
    signout_form: str = 'student/signout-form'
    signin_action: str = 'student/signin'
    signout_action: str = 'student/signout'
    survey_path: str = 'form/survey'            # GET/POST ?course_id=
    exam_path: str = 'test/exam'                # GET/POST ?course_id=
    token_field: str = 'csrf_token'
    form_id_field: str = 'form_id'
    course_field: str = 'course_id'
    user_field: str = 'user'
    password_field: str = 'password'
    serial_field: str = 'reg_id'
    survey_echo_field: str = 'course_echo'      # extra hidden field repeating the course id
    serial_selector: str = 'div.serial'
    material_link_text: str = 'Handout'
    material_hosts: tuple = ()                  # extra hosts allowed for handouts (the site's own host always is)
    labels: dict = field(default_factory=lambda: {
        'signin': 'Check-in', 'signout': 'Check-out', 'survey': 'Survey', 'exam': 'Test score'})
    pending: str = 'Pending'
    stamp_format: str = '%Y-%m-%d %H:%M:%S'     # strptime format of a completed step
    exam_receipt: str = 'Test submitted successfully'
    survey_best_value: str = '5'                # value of the most favourable option in each group
    survey_values: tuple = ('1', '2', '3', '4', '5')

    def url(self, path):
        return urljoin(self.base, path)

    @property
    def tz(self):
        return ZoneInfo(self.timezone)


class SiteError(RuntimeError):
    pass


class FormChanged(SiteError):
    """The page no longer matches the expected structure."""


def text(node):
    return ' '.join(node.get_text(' ', strip=True).split())


def done(profile, value):
    """Fail closed: only a valid timestamp is completed; only the exact pending text is pending."""
    if value == profile.pending:
        return False
    if value is None:
        raise FormChanged('missing_field_value')
    try:
        datetime.strptime(value.strip(), profile.stamp_format)
        return True
    except ValueError:
        raise FormChanged('unknown_field_value') from None


def score(profile, value):
    """None while pending; int for a numeric score; anything else is unknown."""
    if value == profile.pending:
        return None
    if value is None:
        raise FormChanged('missing_score_value')
    if re.fullmatch(r'\d{1,3}', value.strip()) and int(value) <= 100:
        return int(value)
    raise FormChanged('unknown_score_value')


def course_id_of(profile, href):
    """Exactly one non-empty course parameter on the exact course-info URL, else None."""
    u = urlparse(urljoin(profile.base, href))
    target = urlparse(profile.url(profile.course_info_path))
    if (u.scheme, u.netloc, u.path) != (target.scheme, target.netloc, target.path):
        return None
    values = [v for k, v in parse_qsl(u.query, keep_blank_values=True) if k == profile.course_param]
    return values[0] if len(values) == 1 and values[0] else None


def parse_cards(profile, html):
    """{cid: card}. Problems are isolated per course: a broken card becomes {'cid', 'error'}."""
    soup = BeautifulSoup(html, 'html.parser')
    cards = {}
    for node in soup.select('div.card'):
        ids = {course_id_of(profile, a['href']) for a in node.select('a[href]')} - {None}
        if len(ids) != 1:
            continue
        cid = ids.pop()
        if cid in cards:
            cards[cid] = {'cid': cid, 'error': 'duplicate_card'}
            continue
        rows, seen = {}, []
        for tr in node.select('tr'):
            th, td = tr.find('th'), tr.find('td')
            if th and td:
                seen.append(text(th))
                rows[text(th)] = text(td)
        labels = profile.labels
        if any(seen.count(label) != 1 or not rows[label] for label in labels.values()):
            cards[cid] = {'cid': cid, 'error': 'card_status_rows'}
            continue
        serial = node.select_one(profile.serial_selector)
        material = next((urljoin(profile.base, a['href']) for a in node.select('a[href]')
                         if text(a) == profile.material_link_text), None)
        cards[cid] = {'cid': cid, 'serial': text(serial) if serial else None,
                      'fields': {k: rows[v] for k, v in labels.items()}, 'material': material}
    return cards


class Client:
    def __init__(self, profile, secret, session=None, dry_run=False, clock=None):
        self.p = profile
        self.secret = secret
        self.session = session or requests.Session()
        self.dry_run = dry_run
        self.clock = clock or (lambda: datetime.now(profile.tz))
        self.posts = []  # (path, field names) for audit; never values

    # ---- transport -----------------------------------------------------
    def _same_site(self, url):
        return url.startswith(self.p.base)

    def _follow(self, response):
        for _ in range(5):
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            if response.status_code in (307, 308):
                raise SiteError('refused_method_preserving_redirect')
            url = urljoin(response.url, response.headers.get('Location', ''))
            if not self._same_site(url):
                raise SiteError('unexpected_redirect')
            try:
                response = self.session.get(url, timeout=(10, 35), allow_redirects=False)
            except requests.RequestException:
                raise SiteError('network_error') from None
        raise SiteError('too_many_redirects')

    def _get(self, path, authenticated=True):
        url = self.p.url(path)
        if not self._same_site(url):
            raise SiteError('unexpected_destination')
        try:
            response = self.session.get(url, timeout=(10, 35), allow_redirects=False)
        except requests.RequestException:
            raise SiteError('network_error') from None
        response = self._follow(response)
        if response.status_code != 200:
            raise SiteError('http_error')
        soup = BeautifulSoup(response.text, 'html.parser')
        if authenticated and soup.select_one(f'input[name={self.p.password_field}]') \
                and not soup.select_one(f'input[name={self.p.serial_field}]'):
            raise SiteError('session_expired')
        return response.text, soup

    def _send(self, path, data):
        """Credential-bearing POST: never replayed; any failure after it left = outcome unknown."""
        url = self.p.url(path)
        if not self._same_site(url):
            raise SiteError('unexpected_destination')
        try:
            response = self.session.post(url, data=data, timeout=(10, 35), allow_redirects=False)
        except requests.RequestException:
            raise SiteError('post_result_unknown') from None
        try:
            response = self._follow(response)
        except SiteError:
            raise SiteError('post_result_unknown') from None
        if response.status_code != 200:
            raise SiteError('post_http_error')
        return response.text

    def _post(self, path, data, deadline):
        """Mutating POST; suppressed in dry-run; refused after the (inclusive) deadline."""
        if deadline is None or self.clock() > deadline:
            raise SiteError('deadline_passed')
        self.posts.append((path, sorted(data)))
        if self.dry_run:
            return None
        return self._send(path, data)

    # ---- session -------------------------------------------------------
    def login(self):
        """The login POST is allowed in dry-run: it changes nothing on the platform."""
        _, soup = self._get(self.p.login_path, authenticated=False)
        tokens = soup.select(f'input[name={self.p.token_field}]')
        if len(tokens) != 1 or not tokens[0].get('value'):
            raise FormChanged('login_token')
        user, password = self.secret('SITE_USER'), self.secret('SITE_PASSWORD')
        if not user or not password:
            raise SiteError('credentials_unavailable')
        data = {self.p.token_field: tokens[0]['value'], self.p.user_field: user, self.p.password_field: password}
        soup = BeautifulSoup(self._send(self.p.login_path, data), 'html.parser')
        logout = urlparse(self.p.url(self.p.logout_path)).path
        if not any(urlparse(urljoin(self.p.base, a['href'])).path == logout for a in soup.select('a[href]')):
            raise SiteError('login_failed')
        return self

    def cards(self):
        html, _ = self._get(self.p.card_path)
        return parse_cards(self.p, html)

    # ---- forms ---------------------------------------------------------
    def _form(self, soup, action):
        forms = [f for f in soup.select('form') if self.p.url(f.get('action', '')) == self.p.url(action)]
        if len(forms) != 1 or forms[0].get('method', '').lower() != 'post':
            raise FormChanged('form_missing')
        return forms[0]

    @staticmethod
    def _hidden(form):
        return {i['name']: i.get('value', '') for i in form.select('input[type=hidden][name]')}

    @staticmethod
    def _inventory(form, reason):
        out = []
        for el in form.select('[name]'):
            if el.name == 'button':
                raise FormChanged(reason)
            out.append(((el.get('type') or 'text').lower() if el.name == 'input' else el.name, el['name']))
        return out

    def attendance(self, kind, cid, serial, deadline):
        p = self.p
        form_path, action = {'signin': (p.signin_form, p.signin_action),
                             'signout': (p.signout_form, p.signout_action)}[kind]
        _, soup = self._get(f'{form_path}?{p.course_param}={cid}')
        form = self._form(soup, action)
        hidden = self._hidden(form)
        expected = [('hidden', p.token_field), ('hidden', p.course_field), ('email', p.user_field),
                    ('password', p.password_field), ('text', p.serial_field)]
        if sorted(self._inventory(form, kind + '_form_shape')) != sorted(expected) \
                or hidden[p.course_field] != cid or not hidden[p.token_field]:
            raise FormChanged(kind + '_form_shape')
        data = dict(hidden)
        data.update({p.user_field: self.secret('SITE_USER'), p.password_field: self.secret('SITE_PASSWORD'),
                     p.serial_field: serial})
        return self._post(action, data, deadline)

    def survey(self, cid, deadline):
        p = self.p
        path = f'{p.survey_path}?{p.course_param}={cid}'
        _, soup = self._get(path)
        form = self._form(soup, path)
        hidden = self._hidden(form)
        inv = self._inventory(form, 'survey_form_shape')
        hidden_names = [n for k, n in inv if k == 'hidden']
        radios = {n for k, n in inv if k == 'radio'}
        texts = [n for k, n in inv if k == 'textarea']
        if any(k not in ('hidden', 'radio', 'textarea') for k, _ in inv) \
                or sorted(hidden_names) != sorted([p.token_field, p.form_id_field, p.survey_echo_field, p.course_field]) \
                or len(set(texts)) != len(texts) \
                or set(hidden_names) & radios or set(hidden_names) & set(texts) or radios & set(texts) \
                or hidden[p.course_field] != cid or hidden[p.survey_echo_field] != cid \
                or not hidden[p.token_field] or not hidden[p.form_id_field]:
            raise FormChanged('survey_form_shape')
        groups = {}
        for radio in form.select('input[type=radio][name]'):
            groups.setdefault(radio['name'], []).append(radio.get('value'))
        if not groups or any(sorted(v) != sorted(p.survey_values) for v in groups.values()):
            raise FormChanged('survey_radio_shape')
        data = dict(hidden)
        data.update({name: p.survey_best_value for name in groups})
        data.update({t: '' for t in texts})
        return self._post(path, data, deadline)

    def exam(self, cid):
        p = self.p
        path = f'{p.exam_path}?{p.course_param}={cid}'
        _, soup = self._get(path)
        form = self._form(soup, path)
        hidden = self._hidden(form)
        inv = self._inventory(form, 'exam_form_shape')
        hidden_names = [n for k, n in inv if k == 'hidden']
        selects = [n for k, n in inv if k == 'select']
        if any(k not in ('hidden', 'select') for k, _ in inv) \
                or sorted(hidden_names) != sorted([p.token_field, p.form_id_field, p.course_field]) \
                or len(set(selects)) != len(selects) or set(selects) & set(hidden_names) \
                or hidden[p.course_field] != cid or not hidden[p.token_field] or not hidden[p.form_id_field]:
            raise FormChanged('exam_form_shape')
        questions = []
        for select in form.select('select[name]'):
            no = select['name']
            if not re.fullmatch(r'\d{1,3}', no):
                raise FormChanged('exam_question_name')
            options = {o.get('value'): text(o) for o in select.select('option') if o.get('value')}
            if len(options) < 2 or not all(re.fullmatch(r'[A-Z]', k) for k in options):
                raise FormChanged('exam_options')
            row = select.find_parent('tr')
            prev = row.find_previous_sibling('tr') if row else None
            questions.append({'no': no, 'text': text(prev) if prev else '', 'options': options})
        if not questions:
            raise FormChanged('exam_questions')
        return {'hidden': hidden, 'questions': questions, 'path': path}

    def submit_exam(self, paper, answers, deadline):
        """True only when the platform's own success message is in the response."""
        expected = {q['no']: q['options'] for q in paper['questions']}
        if set(answers) != set(expected) or any(answers[n] not in expected[n] for n in expected):
            raise SiteError('invalid_answers')
        data = dict(paper['hidden'])
        data.update(answers)
        body = self._post(paper['path'], data, deadline)
        return body is not None and self.p.exam_receipt in body
