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

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from django.contrib.auth import get_user_model

from apps.accounts.auth import InitDataError, verify_init_data, verify_login_widget

User = get_user_model()

BOT_TOKEN = "123456:TEST-TOKEN-FOR-SIGNING"
TOKEN = BOT_TOKEN  # alias used by the Login Widget tests below


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


class LoginWidgetTests(TestCase):
    """
    Telegram's web Login Widget. Signs with the same bot token as the Mini App
    but derives the key differently — plain SHA256 rather than HMAC keyed on
    "WebAppData" — and swapping them rejects every legitimate login.
    """

    def _signed(self, token=TOKEN, **fields):
        data = {"id": 555, "first_name": "Web", "username": "webuser",
                "auth_date": int(time.time()), **fields}
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hashlib.sha256(token.encode()).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return data

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_a_valid_payload_verifies(self):
        payload = verify_login_widget(self._signed())
        self.assertEqual(payload["id"], 555)

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_a_tampered_field_is_rejected(self):
        data = self._signed()
        data["id"] = 999           # someone else's account
        with self.assertRaises(InitDataError):
            verify_login_widget(data)

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_a_payload_signed_with_another_token_is_rejected(self):
        with self.assertRaises(InitDataError):
            verify_login_widget(self._signed(token="999:WRONGTOKEN"))

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_a_stale_login_is_rejected(self):
        """The signature never expires on its own; auth_date is the only guard."""
        old = self._signed(auth_date=int(time.time()) - 60 * 60 * 48)
        with self.assertRaises(InitDataError):
            verify_login_widget(old)

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_missing_hash_is_rejected(self):
        data = self._signed()
        del data["hash"]
        with self.assertRaises(InitDataError):
            verify_login_widget(data)

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_mini_app_derivation_does_not_validate_a_widget_payload(self):
        """The two key derivations are not interchangeable — this is the trap."""
        data = self._signed()
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash")
        wrong_secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        wrong = hmac.new(wrong_secret, check.encode(), hashlib.sha256).hexdigest()
        self.assertNotEqual(wrong, data["hash"])


class LoginWidgetEndpointTests(APITestCase):
    def _signed(self, **fields):
        data = {"id": 777, "first_name": "Web", "username": "web777",
                "auth_date": int(time.time()), **fields}
        check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hashlib.sha256(TOKEN.encode()).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return data

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_web_login_issues_tokens_and_creates_the_account(self):
        response = self.client.post(
            reverse("accounts:telegram-login"),
            {"telegram_login": self._signed()}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertTrue(User.objects.filter(telegram_id=777).exists())

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_bot_and_web_resolve_to_one_account(self):
        """
        The point of keying on telegram_id: someone who starts in the bot and
        later signs in on the web keeps their wallet and credits.
        """
        from apps.accounts import services

        services.get_or_create_from_telegram(telegram_id=777, username="web777")
        self.client.post(
            reverse("accounts:telegram-login"),
            {"telegram_login": self._signed()}, format="json",
        )
        self.assertEqual(User.objects.filter(telegram_id=777).count(), 1)

    @override_settings(TELEGRAM_BOT_TOKEN=TOKEN)
    def test_a_forged_payload_is_refused(self):
        forged = self._signed()
        forged["id"] = 12345
        response = self.client.post(
            reverse("accounts:telegram-login"),
            {"telegram_login": forged}, format="json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(telegram_id=12345).exists())
