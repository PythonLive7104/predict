"""
Connecting a browser to a Telegram account.

The code is the entire credential between a browser and an account, so the
security lives in three properties: unguessable, short-lived, single use. Each
has a test, because losing any one turns this into an account-takeover route.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts import services
from apps.accounts.models import LinkCode

User = get_user_model()


def _user(tid=4242, username="tester"):
    user, _ = services.get_or_create_from_telegram(telegram_id=tid, username=username)
    return user


class LinkCodeTests(TestCase):
    def test_codes_are_long_and_unique(self):
        codes = {services.create_link_code().code for _ in range(50)}
        self.assertEqual(len(codes), 50)
        self.assertTrue(all(len(c) >= 30 for c in codes), "too short to resist guessing")

    def test_claim_then_redeem_returns_the_account(self):
        code = services.create_link_code()
        user = _user()

        self.assertTrue(services.claim_link_code(code.code, user))
        self.assertEqual(services.redeem_link_code(code.code), user)

    def test_a_code_is_single_use(self):
        """A code read off a shared screen must not still be worth a session."""
        code = services.create_link_code()
        services.claim_link_code(code.code, _user())

        self.assertIsNotNone(services.redeem_link_code(code.code))
        self.assertIsNone(services.redeem_link_code(code.code))

    def test_an_unclaimed_code_redeems_to_nothing(self):
        code = services.create_link_code()
        self.assertIsNone(services.redeem_link_code(code.code))

    def test_an_expired_code_cannot_be_claimed_or_redeemed(self):
        code = services.create_link_code()
        LinkCode.objects.filter(pk=code.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        self.assertFalse(services.claim_link_code(code.code, _user()))
        self.assertIsNone(services.redeem_link_code(code.code))

    def test_a_claimed_code_cannot_be_reclaimed_by_someone_else(self):
        """Otherwise a second person tapping the link steals the session."""
        code = services.create_link_code()
        first = _user(1111, "first")
        second = _user(2222, "second")

        self.assertTrue(services.claim_link_code(code.code, first))
        self.assertFalse(services.claim_link_code(code.code, second))
        self.assertEqual(services.redeem_link_code(code.code), first)

    def test_an_unknown_code_is_refused(self):
        self.assertFalse(services.claim_link_code("nope", _user()))
        self.assertIsNone(services.redeem_link_code("nope"))


class LinkEndpointTests(APITestCase):
    def test_start_returns_a_code_and_a_bot_link(self):
        with self.settings(TELEGRAM_BOT_USERNAME="profheist001_bot"):
            response = self.client.post(reverse("accounts:link-start"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("t.me/profheist001_bot?start=", response.data["bot_url"])
        self.assertIn(response.data["code"], response.data["bot_url"])

    def test_polling_an_unclaimed_code_says_pending(self):
        code = services.create_link_code()
        response = self.client.get(reverse("accounts:link-status", args=[code.code]))
        self.assertEqual(response.status_code, 202)

    def test_polling_after_the_bot_claims_it_returns_a_session(self):
        code = services.create_link_code()
        user = _user()
        services.claim_link_code(code.code, user)

        response = self.client.get(reverse("accounts:link-status", args=[code.code]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertEqual(response.data["user"]["referral_code"], user.referral_code)

    def test_polling_twice_does_not_hand_out_a_second_session(self):
        code = services.create_link_code()
        services.claim_link_code(code.code, _user())

        self.client.get(reverse("accounts:link-status", args=[code.code]))
        again = self.client.get(reverse("accounts:link-status", args=[code.code]))

        self.assertEqual(again.status_code, 404)

    def test_unknown_expired_and_spent_codes_all_answer_alike(self):
        """
        Distinguishing them tells someone guessing codes which guesses landed
        close, which is most of the work of guessing.
        """
        spent = services.create_link_code()
        services.claim_link_code(spent.code, _user())
        self.client.get(reverse("accounts:link-status", args=[spent.code]))

        expired = services.create_link_code()
        LinkCode.objects.filter(pk=expired.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        for code in ("never-existed", spent.code, expired.code):
            response = self.client.get(reverse("accounts:link-status", args=[code]))
            self.assertEqual(response.status_code, 404, code)
