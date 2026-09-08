"""
initData verification.

This is the front door for the Mini App: a forgery here logs an attacker in as
any user they name. Every test below is an attack, not a happy path.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from django.test import SimpleTestCase, override_settings

from apps.accounts.auth import InitDataError, verify_init_data

BOT_TOKEN = "123456:TEST-TOKEN-FOR-SIGNING"


def sign(payload: dict, token: str = BOT_TOKEN) -> str:
    """Build a correctly signed initData string, the way Telegram does."""
    data = {k: v for k, v in payload.items() if k != "hash"}
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def payload(**overrides) -> dict:
    base = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF_test",
        "user": json.dumps({"id": 4242, "first_name": "Ada", "username": "ada"}),
    }
    base.update(overrides)
    return base


@override_settings(TELEGRAM_BOT_TOKEN=BOT_TOKEN)
class VerifyInitDataTests(SimpleTestCase):
    def test_valid_init_data_is_accepted_and_parsed(self):
        result = verify_init_data(sign(payload()))
        self.assertEqual(result["user"]["id"], 4242)
        self.assertEqual(result["user"]["username"], "ada")

    def test_tampered_user_is_rejected(self):
        """The core attack: keep a real signature, swap in someone else's id."""
        signed = sign(payload())
        forged = signed.replace("4242", "9999")
        with self.assertRaises(InitDataError):
            verify_init_data(forged)

    def test_signature_from_a_different_bot_token_is_rejected(self):
        with self.assertRaises(InitDataError):
            verify_init_data(sign(payload(), token="999:SOMEONE-ELSES-BOT"))

    def test_missing_hash_is_rejected(self):
        with self.assertRaises(InitDataError):
            verify_init_data(urlencode(payload()))

    def test_empty_string_is_rejected(self):
        with self.assertRaises(InitDataError):
            verify_init_data("")

    def test_stale_init_data_is_rejected(self):
        old = payload(auth_date=str(int(time.time()) - 60 * 60 * 48))
        with self.assertRaises(InitDataError):
            verify_init_data(sign(old))

    def test_fresh_init_data_within_the_window_is_accepted(self):
        recent = payload(auth_date=str(int(time.time()) - 60))
        self.assertEqual(verify_init_data(sign(recent))["user"]["id"], 4242)

    def test_appending_an_unsigned_field_is_rejected(self):
        """Extra fields change the check string, so the hash must stop matching."""
        signed = sign(payload())
        with self.assertRaises(InitDataError):
            verify_init_data(signed + "&is_premium=true")

    def test_start_param_is_covered_by_the_signature(self):
        """start_param carries the referral code — it must not be forgeable."""
        signed = sign(payload(start_param="REFCODE1"))
        self.assertEqual(verify_init_data(signed)["start_param"], "REFCODE1")

        with self.assertRaises(InitDataError):
            verify_init_data(signed.replace("REFCODE1", "OTHERREF"))


class MisconfigurationTests(SimpleTestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_unset_bot_token_refuses_rather_than_accepting_anything(self):
        """A blank token must never degrade into 'everything verifies'."""
        with self.assertRaises(InitDataError):
            verify_init_data(sign(payload()))
