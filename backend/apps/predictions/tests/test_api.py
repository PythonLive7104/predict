"""
API gating.

The product is the selection. If any of these leak `selection`, `selection_label`,
`rationale` or `market_odds` to someone who has not unlocked the pick, there is
no product — so these assert on the raw response payload, not on the UI.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.billing.models import Plan, Subscription, Wallet
from apps.predictions.models import Market, Outcome, Prediction, Tier

from .factories import make_fixture, make_league, make_team

User = get_user_model()

GATED_FIELDS = ("selection", "selection_label", "market_odds")


class ApiTestBase(APITestCase):
    def setUp(self):
        self.league = make_league()
        self.user = User.objects.create(username="tg_api", telegram_id=9001)
        Wallet.objects.create(user=self.user, balance=5)

    def make_pick(self, confidence=80, tier=Tier.FREE, market=Market.MATCH_RESULT,
                  selection="home", odds="1.80"):
        stamp = timezone.now().microsecond + confidence
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"Home{stamp}"),
            away=make_team(f"Away{stamp}"),
        )
        return Prediction.objects.create(
            fixture=fixture, market=market, selection=selection,
            probability=confidence / 100, confidence=confidence,
            market_odds=Decimal(odds), rationale="Because the numbers say so.",
            tier=tier, published_at=timezone.now(),
        )

    def subscribe(self, unlimited=True, vip=True):
        plan = Plan.objects.create(
            code=f"p{unlimited}{vip}", name="Test Plan", price_usd=Decimal("10"),
            duration_days=30, unlimited=unlimited, includes_vip_slips=vip,
        )
        Subscription.objects.create(
            user=self.user, plan=plan, expires_at=timezone.now() + timedelta(days=10)
        )


class LockedPickTests(ApiTestBase):
    def test_anonymous_never_receives_a_selection(self):
        self.make_pick()
        response = self.client.get(reverse("predictions:todays-picks"))

        self.assertEqual(response.status_code, 200)
        pick = response.data["results"][0]
        for field in GATED_FIELDS:
            self.assertIsNone(pick[field], f"{field} leaked to anonymous")
        self.assertEqual(pick["rationale"], "")
        self.assertFalse(pick["unlocked"])

    def test_confidence_and_fixture_stay_public(self):
        """The teaser has to be useful, or nobody converts."""
        self.make_pick(confidence=77)
        pick = self.client.get(reverse("predictions:todays-picks")).data["results"][0]

        self.assertEqual(pick["confidence"], 77)
        self.assertEqual(pick["market_label"], "Match Result (1X2)")
        self.assertIn("Home", pick["fixture"]["home"])

    def test_authenticated_but_unpaid_user_sees_nothing_extra(self):
        self.make_pick()
        self.client.force_authenticate(self.user)
        pick = self.client.get(reverse("predictions:todays-picks")).data["results"][0]

        for field in GATED_FIELDS:
            self.assertIsNone(pick[field])

    def test_unlocking_one_pick_does_not_unlock_the_others(self):
        first, second = self.make_pick(confidence=80), self.make_pick(confidence=70)
        self.client.force_authenticate(self.user)
        self.client.post(reverse("predictions:unlock-pick", args=[first.pk]))

        results = {
            p["id"]: p
            for p in self.client.get(reverse("predictions:todays-picks")).data["results"]
        }

        self.assertTrue(results[first.pk]["unlocked"])
        self.assertIsNotNone(results[first.pk]["selection"])
        self.assertFalse(results[second.pk]["unlocked"])
        self.assertIsNone(results[second.pk]["selection"])


class UnlockTests(ApiTestBase):
    def test_unlock_requires_authentication(self):
        pick = self.make_pick()
        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))
        self.assertIn(response.status_code, (401, 403))

    def test_unlock_spends_exactly_one_credit_and_returns_the_pick(self):
        pick = self.make_pick()
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["credits"], 4)
        self.assertEqual(response.data["prediction"]["selection"], "home")
        self.assertTrue(response.data["prediction"]["unlocked"])

    def test_repeat_unlock_does_not_charge_again(self):
        pick = self.make_pick()
        self.client.force_authenticate(self.user)
        self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))
        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))

        self.assertEqual(response.data["status"], "already")
        self.assertEqual(response.data["credits"], 4)

    def test_out_of_credits_returns_402_and_no_selection(self):
        Wallet.objects.filter(user=self.user).update(balance=0)
        pick = self.make_pick()
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.data["code"], "insufficient_credits")
        self.assertNotIn("prediction", response.data)

    def test_unknown_pick_is_404(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse("predictions:unlock-pick", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_unpublished_pick_cannot_be_unlocked(self):
        """A generated-but-unpublished pick must not be reachable by guessing an id."""
        pick = self.make_pick()
        Prediction.objects.filter(pk=pick.pk).update(published_at=None)
        self.client.force_authenticate(self.user)

        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Wallet.objects.get(user=self.user).balance, 5)

    def test_unlimited_plan_needs_no_credits(self):
        self.subscribe()
        Wallet.objects.filter(user=self.user).update(balance=0)
        pick = self.make_pick()
        self.client.force_authenticate(self.user)

        response = self.client.post(reverse("predictions:unlock-pick", args=[pick.pk]))
        self.assertEqual(response.data["status"], "already")
        self.assertEqual(response.data["prediction"]["selection"], "home")

    def test_unlimited_plan_sees_every_pick_open(self):
        self.subscribe()
        self.make_pick(confidence=80)
        self.make_pick(confidence=70, tier=Tier.VIP)
        self.client.force_authenticate(self.user)

        results = self.client.get(reverse("predictions:todays-picks")).data["results"]
        self.assertTrue(all(p["unlocked"] for p in results))
        self.assertTrue(all(p["selection"] for p in results))


class SlipTests(ApiTestBase):
    def _slip(self):
        from apps.predictions.models import Slip

        pick = self.make_pick()
        slip = Slip.objects.create(
            kind="banker", title="Banker", for_date=timezone.now().date(),
            published_at=timezone.now(), total_odds=Decimal("1.80"),
        )
        slip.predictions.set([pick])
        return slip

    def test_non_subscriber_gets_the_shape_but_not_the_legs(self):
        self._slip()
        response = self.client.get(reverse("predictions:todays-slips"))

        self.assertFalse(response.data["vip"])
        leg = response.data["results"][0]["predictions"][0]
        for field in GATED_FIELDS:
            self.assertIsNone(leg[field], f"{field} leaked in a slip leg")

    def test_vip_subscriber_sees_the_legs(self):
        self._slip()
        self.subscribe(unlimited=False, vip=True)
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("predictions:todays-slips"))
        self.assertTrue(response.data["vip"])
        self.assertEqual(response.data["results"][0]["predictions"][0]["selection"], "home")

    def test_expired_subscription_loses_vip_access(self):
        self._slip()
        self.subscribe(unlimited=False, vip=True)
        Subscription.objects.filter(user=self.user).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("predictions:todays-slips"))
        self.assertFalse(response.data["vip"])
        self.assertIsNone(response.data["results"][0]["predictions"][0]["selection"])


class RecordTests(ApiTestBase):
    def test_record_is_public(self):
        response = self.client.get(reverse("predictions:record"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", response.data)

    def test_record_counts_losses_too(self):
        """A record that only counts winners is worthless — and dishonest."""
        won = self.make_pick(confidence=80)
        lost = self.make_pick(confidence=70)
        Prediction.objects.filter(pk=won.pk).update(outcome=Outcome.WON)
        Prediction.objects.filter(pk=lost.pk).update(outcome=Outcome.LOST)

        summary = self.client.get(reverse("predictions:record")).data["summary"]
        self.assertEqual(summary["settled"], 2)
        self.assertEqual(summary["won"], 1)
        self.assertEqual(summary["lost"], 1)
        self.assertEqual(summary["win_rate"], 50.0)


class ProfileTests(ApiTestBase):
    def test_me_requires_authentication(self):
        self.assertIn(self.client.get(reverse("accounts:me")).status_code, (401, 403))

    def test_notifications_can_be_toggled(self):
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            reverse("accounts:me"), {"notifications_enabled": False}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["notifications_enabled"])

        self.user.refresh_from_db()
        self.assertFalse(self.user.notifications_enabled)

    def test_non_boolean_toggle_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            reverse("accounts:me"), {"notifications_enabled": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.notifications_enabled)

    def test_plan_fields_are_not_client_settable(self):
        """Entitlements come from subscriptions — never from the request body."""
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            reverse("accounts:me"),
            {"notifications_enabled": True, "unlimited": True, "credits": 9999,
             "plan": "Pro"},
            format="json",
        )

        self.assertEqual(response.data["plan"], "Free")
        self.assertFalse(response.data["unlimited"])
        self.assertEqual(response.data["credits"], 5)
