"""config.toml + secrets.env loading. Secrets never live in config.toml."""
import html
import html.entities
import json
import os
import re
import tomllib
from dataclasses import fields
from functools import lru_cache
from pathlib import Path

from .site import SiteProfile

SECRET_KEYS = ('SITE_PASSWORD', 'TELEGRAM_BOT_TOKEN', 'GEMINI_API_KEY', 'OPENAI_API_KEY',
               'ANTHROPIC_API_KEY', 'XAI_API_KEY')


def load_config(path):
    path = Path(path).expanduser().resolve()
    cfg = tomllib.loads(path.read_text())
    base = path.parent
    for key in ('state_dir', 'registrations', 'secrets_file'):
        cfg['paths'][key] = str((base / Path(cfg['paths'][key]).expanduser()).resolve())
    allowed = {f.name for f in fields(SiteProfile)}
    unknown = set(cfg['site']) - allowed
    if unknown:
        raise ValueError(f'unknown [site] keys: {sorted(unknown)}')
    cfg['profile'] = SiteProfile(**cfg['site'])
    cfg['_path'] = str(path)
    if not cfg.get('answer', {}).get('providers'):
        raise ValueError('configure at least one [[answer.providers]] entry')
    return cfg


def load_secrets(path):
    """KEY=VALUE lines; environment variables override the file."""
    result = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    return lambda key: os.environ.get(key) or result.get(key)


@lru_cache(maxsize=32)
def html_pattern(hidden):
    """Regex for `hidden` as a page may print it: any character may be an HTML entity
    (named, decimal or hex, escaped or not, in any mix)."""
    def char(c):
        forms = [re.escape(c), f'&#0*{ord(c)}(?:;|(?![0-9]))', f'&#[xX]0*(?i:{ord(c):x})(?:;|(?![0-9a-fA-F]))']
        forms += [re.escape('&' + name) for name, v in html.entities.html5.items() if v == c]
        return '(?:' + '|'.join(forms) + ')'
    return re.compile(''.join(char(c) for c in hidden))


def redact(value, secret):
    """Replace secrets, including their JSON-escaped and HTML-escaped forms (pages may echo a value)."""
    value = str(value)
    if secret:
        for key in SECRET_KEYS:
            hidden = secret(key)
            if hidden:
                forms = {hidden, json.dumps(hidden)[1:-1], json.dumps(hidden, ensure_ascii=False)[1:-1]}
                for form in sorted(forms, key=len, reverse=True):     # longest first: no partial leftovers
                    value = value.replace(form, '***')
                value = html_pattern(hidden).sub('***', value)
    return value
