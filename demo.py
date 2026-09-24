#!/usr/bin/env python3
"""Fast-forward one class day against the bundled mock platform (no network, no API key).

    python demo.py            # happy path: register -> check-in -> check-out -> survey -> test
    python demo.py --wrong    # the static answerer picks wrong answers: see retry + manual alert
"""
import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from autopilot import register, runner
from autopilot.config import load_config, load_secrets
from mock_site.server import PASSWORD, TZ, USER, Platform, serve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wrong', action='store_true')
    args = ap.parse_args()
    day = datetime.now(TZ).date()
    sim = {'now': datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)}
    clock = lambda: sim['now']   # noqa: E731
    platform = Platform(clock=clock, day=day)
    server = serve(platform)
    port = server.server_address[1]
    work = Path(tempfile.mkdtemp(prefix='autopilot-demo-'))
    (work / 'secrets.env').write_text(f'SITE_USER={USER}\nSITE_PASSWORD={PASSWORD}\n')
    (work / 'config.toml').write_text(f'''[paths]
state_dir = "state"
registrations = "registrations.json"
secrets_file = "secrets.env"
[site]
base = "http://127.0.0.1:{port}/app/"
[notify]
type = "stdout"
[[answer.providers]]
type = "static"
letter = "{'B' if args.wrong else 'A'}"
''')
    cfg = load_config(work / 'config.toml')
    secret = load_secrets(cfg['paths']['secrets_file'])
    print(f'workdir {work}\n--- 08:00 registration robot')
    for line in register.run(cfg, now=sim['now']):
        print('   ', line)

    state = {}
    for hhmm in ('08:30', '13:51', '15:51', '15:52', '15:53', '16:31'):
        h, m = map(int, hhmm.split(':'))
        sim['now'] = sim['now'].replace(hour=h, minute=m)
        print(f'--- {hhmm} tick')
        regs, _ = runner.load_registrations(cfg['paths']['registrations'])
        courses, _ = runner.select_courses(regs, sim['now'])
        courses = [c for c in courses if not (state.get('courses', {}).get(c['cid']) or {}).get('closed')]
        tick = runner.Tick(state, secret, sim['now'], clock=clock, profile=cfg['profile'], policy={},
                           send=lambda text: print('    [notify]', text) or True,
                           agents=runner.Agents(cfg, cfg['_path'], clock))
        tick.run(courses)
        runner.check_state(json.loads(json.dumps(state)))
    print('--- final student card')
    card = state['courses']['DEMO101']
    print('   ', {s: bool(v['done']) for s, v in card['steps'].items()}, 'graded:', card['exam']['graded'])
    server.shutdown()


if __name__ == '__main__':
    main()
