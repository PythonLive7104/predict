"""
Telegram Mini App authentication.

The Mini App hands the page an `initData` query string signed by Telegram with a
key derived from the bot token. Verifying it proves the user is who Telegram says
without a password — which is the whole reason one React build can serve both the
public web app (email + JWT) and the in-Telegram app.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
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
