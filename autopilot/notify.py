"""Notification back-ends. Delivery failures are retried by the runner's durable outbox."""
import requests


def make_notifier(cfg, secret):
    kind = (cfg or {}).get('type', 'stdout')
    if kind == 'telegram':
        def send(text):
            token, chat = secret(cfg.get('token_key', 'TELEGRAM_BOT_TOKEN')), secret(cfg.get('chat_key', 'TELEGRAM_CHAT_ID'))
            if not token or not chat:
                return False
            try:
                r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                  data={'chat_id': chat, 'text': text}, timeout=20, allow_redirects=False)
                return r.ok and r.json().get('ok') is True
            except Exception:
                return False
        return send
    if kind == 'stdout':
        def send(text):
            print('[notify]', text, flush=True)
            return True
        return send
    raise ValueError(f'unknown notifier {kind}')
