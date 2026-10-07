# gcp-course-autopilot (English summary)

A **technical case study** built around a bundled **mock e-learning platform**: a time-windowed, multi-step,
no-redo web-form workflow (registration → check-in → check-out → survey → multiple-choice test), focusing on
**how to stop safely, avoid duplicate submissions and verify every step**.

**This is not an attendance or exam proxy.** Check-in records must not replace actual attendance; tests tied to
qualifications, credits or certification should be completed by the person according to their institution's rules.
Only a mock platform is included; obtain the applicable authorisation before connecting anything real.
See `docs/responsible-use.md`.

- Platform details for the attendance flow live in `config.toml` (`[site]` = `autopilot.site.SiteProfile`);
  `register.py` is a reference implementation for the mock platform.
- Test answering providers are interchangeable (`static` demo, Gemini / OpenAI / Anthropic / xAI HTTP APIs, or any
  agent CLI). **No model is hard-coded**; you must name one.
- A fallback agent CLI may take over once when a page changes. It is instructed to use only `browser_helper.py`,
  which fills credentials itself and limits, at the network layer, each run to one course, one step and one
  submission. That is a behavioural guardrail, **not isolation**.
- A step counts as done only when the status page shows a valid timestamp (or a 0–100 score; a score followed by the
  retake button text `retake_text`, e.g. `90 Retake`, counts as 90, while e.g. `Pending Retake` still stops). A durable
  "pending" record is written before each POST; outcome-unknown submissions are never resent. `--dry-run` writes no
  local files and sends no state-changing form, but still logs in once.
- The survey waits `survey_delay_minutes` (default 3) after the check-out time shown on the card, unless that would
  leave less than `survey_min_left_minutes` (default 15) before the window ends (the delay is a rule of thumb, not a
  guarantee). A survey reply the card ignores is retried once quietly and alerts on the second miss; a submission
  error alerts at once; each step alerts once.
- Pages the program cannot use (unrecognised or expired cards, the reply after a submission the card ignored) are
  saved with secrets masked (including JSON- and HTML-escaped forms) under `<state_dir>/diag/` (directory 0700,
  files 0600). Only the secret keys are masked (passwords and API keys), not the account name or other page data;
  nothing is saved in `--dry-run`, a failed write never stops a run, and old files are not cleaned up automatically.

```bash
# Python 3.11+, macOS/Linux
pip install -r requirements.txt
python demo.py && python demo.py --wrong
python -m unittest discover -s tests -t .   # 118 tests, all against the local mock platform
```

Docs (Traditional Chinese): `docs/design.md`, `docs/lessons.md`, `docs/responsible-use.md`.
License: MIT.
