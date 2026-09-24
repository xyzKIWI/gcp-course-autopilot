"""Registration robot (reference implementation for the bundled mock platform).

Registration is the most platform-specific part, so this module is a pattern to adapt,
not a universal client. The safety rules are the point:

* only courses that pass the eligibility policy (free, online, registration window open,
  and your own schedule check) are submitted;
* one course per run; an *intent* is written to disk before the POST;
* success means the platform's own registration list shows the course with a serial;
* an intent without a confirmed registration is never retried blindly: it is reported;
* the output file is exactly what runner.py reads (registrations.json).

    python -m autopilot.register --config config.toml [--dry-run]
"""
import argparse
import fcntl
import json
import os
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .config import load_config, load_secrets
from .notify import make_notifier
from .site import Client, SiteError


def parse_courses(html):
    soup = BeautifulSoup(html, 'html.parser')
    out = {}
    for tr in soup.select('table.courses tr'):
        cell = {td.get('class', [''])[0]: td.get_text(strip=True) for td in tr.find_all('td')}
        if not cell.get('cid'):
            continue
        rs, re_ = cell['reg'].split('|')
        ci, co = cell['checkin'].split('|'), cell['checkout'].split('|')
        out[cell['cid']] = {'title': cell['title'], 'mode': cell['mode'], 'fee': float(cell['fee']),
                            'reg_start': rs, 'reg_end': re_, 'checkin_start': ci[0], 'checkin_end': ci[1],
                            'checkout_start': co[0], 'checkout_end': co[1]}
    return out


def parse_registrations(html):
    soup = BeautifulSoup(html, 'html.parser')
    return {tr.select_one('td.cid').get_text(strip=True): tr.select_one('td.serial').get_text(strip=True)
            for tr in soup.select('table.registrations tr') if tr.select_one('td.cid')}


def eligible(course, now, schedule_ok=lambda course: True):
    """Default policy: free, online, window open, and your own schedule check (e.g. shifts)."""
    return (course['fee'] == 0 and course['mode'] == 'online'
            and datetime.fromisoformat(course['reg_start']) <= now <= datetime.fromisoformat(course['reg_end'])
            and schedule_ok(course))


def hhmm(a, b):
    return f"{datetime.fromisoformat(a):%H:%M}–{datetime.fromisoformat(b):%H:%M}"


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def run(cfg, dry_run=False, client=None, now=None, send=None, schedule_ok=lambda c: True):
    profile = cfg['profile']
    path = Path(cfg['paths']['registrations'])
    data = json.loads(path.read_text()) if path.exists() else {'courses': {}, 'intents': {}}
    secret = load_secrets(cfg['paths']['secrets_file'])
    send = send or make_notifier(cfg.get('notify'), secret)
    now = now or datetime.now(profile.tz)
    client = client or Client(profile, secret, dry_run=dry_run).login()
    html, _ = client._get('courses', authenticated=False)
    offered = parse_courses(html)
    registered = parse_registrations(client._get('student/registrations')[0])
    report, attempted = [], False
    for cid, course in sorted(offered.items()):
        record = data['courses'].setdefault(cid, {})
        record.update(title=course['title'], mode=course['mode'],
                      checkin=hhmm(course['checkin_start'], course['checkin_end']),
                      checkout=hhmm(course['checkout_start'], course['checkout_end']),
                      **{k: course[k] for k in ('checkin_start', 'checkin_end', 'checkout_start', 'checkout_end')})
        if cid in registered:
            record.update(status='registered', serial=registered[cid])
            data['intents'].pop(cid, None)
            continue
        if cid in data['intents']:
            report.append(f'❌ {cid} 上次送出報名但沒查到結果；為避免重複報名，請手動確認。')
            continue
        if attempted or not eligible(course, now, schedule_ok):
            continue   # at most one submission attempt per run, whatever its outcome
        attempted = True
        if dry_run:
            report.append(f'📝 would register {cid}')
            continue
        data['intents'][cid] = now.isoformat()
        save(path, data)                                    # intent is durable before the POST
        _, soup = client._get(f'student/register-form?course_id={cid}')
        form = client._form(soup, 'student/register')
        hidden = client._hidden(form)
        if sorted(client._inventory(form, 'register_form_shape')) != \
                sorted([('hidden', profile.token_field), ('hidden', profile.course_field)]) \
                or hidden.get(profile.course_field) != cid or not hidden.get(profile.token_field):
            data['intents'].pop(cid)                        # nothing was sent
            report.append(f'❌ {cid} 報名表單結構或課程代碼不符，未送出，請手動確認。')
            continue
        try:
            client._post('student/register', hidden, deadline=datetime.fromisoformat(course['reg_end']))
        except SiteError as exc:
            report.append(f'⚠️ {cid} 報名送出狀態不明（{exc}），已記錄意圖，不會自動重送。')
            continue
        registered = parse_registrations(client._get('student/registrations')[0])
        if cid in registered:
            record.update(status='registered', serial=registered[cid])
            data['intents'].pop(cid)
            report.append(f'✅ 已報名 {cid}「{course["title"]}」，序號 {registered[cid]}')
        else:
            report.append(f'❌ {cid} 送出後查不到報名紀錄，請手動確認。')
    if not dry_run:
        save(path, data)
    for line in report:
        if not dry_run:
            send(line)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    lock_path = Path(cfg['paths']['registrations']).with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # no overlapping registration runs
        except BlockingIOError:
            return 0
        for line in run(cfg, dry_run=a.dry_run):
            print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
