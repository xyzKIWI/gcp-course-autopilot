import sys

if sys.version_info < (3, 11):   # tomllib
    raise SystemExit('autopilot requires Python 3.11+ on macOS or Linux (POSIX file locks)')
