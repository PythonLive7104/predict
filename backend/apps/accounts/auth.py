"""
Telegram authentication, in two flavours.

Both prove a user is who Telegram says without a password, and both sign with
the bot token — but they derive the key differently, and swapping them silently
rejects every login:

* **Mini App** (`verify_init_data`) — a query string handed to the page inside
  Telegram. Key is HMAC-SHA256("WebAppData", token).
* **Login Widget** (`verify_login_widget`) — a JSON object from Telegram's
  web login button. Key is plain SHA256(token).

References:
  https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
  https://core.telegram.org/widgets/login#checking-authorization
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from django.conf import settings

# Telegram's signature covers a timestamp; anything older is a replay.
MAX_AGE_SECONDS = 24 * 60 * 60


class InitDataError(ValueError):
    pass


def verify_init_data(init_data: str, max_age: int = MAX_AGE_SECONDS) -> dict:
    """
    Returns the parsed payload (including a decoded `user` dict) or raises.

    Never trust any field before the hash checks out — `user` is attacker-supplied
    until then, and it carries the telegram_id we log people in as.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        raise InitDataError("TELEGRAM_BOT_TOKEN is not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise InitDataError("missing hash")

    # Fields sorted by key, joined with newlines — Telegram's exact format.
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))

    secret_key = hmac.new(
        b"WebAppData", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("signature mismatch")

    auth_date = int(pairs.get("auth_date", "0"))
    if max_age and (time.time() - auth_date) > max_age:
        raise InitDataError("initData expired")

    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


def verify_login_widget(data: dict, max_age: int = MAX_AGE_SECONDS) -> dict:
    """
    Verify a payload from Telegram's Login Widget and return it.

    Note the key derivation differs from the Mini App: here it is a plain
    SHA256 of the bot token, not an HMAC keyed on "WebAppData". Using the Mini
    App derivation rejects every legitimate widget login with a signature
    mismatch, which looks identical to an attack.

    As with initData: trust no field until the hash checks out. `id` is the
    telegram_id we log someone in as, and it is attacker-supplied until then.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        raise InitDataError("TELEGRAM_BOT_TOKEN is not configured")

    pairs = {k: v for k, v in data.items() if k != "hash" and v is not None}
    received_hash = data.get("hash") or ""
    if not received_hash:
        raise InitDataError("missing hash")

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("signature mismatch")

    auth_date = int(pairs.get("auth_date", 0) or 0)
    if max_age and (time.time() - auth_date) > max_age:
        raise InitDataError("login expired")

    return pairs
