"""Audience selection and broadcast content — the two ways push goes wrong."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.bot.notifications import (
    audience,
    build_free_picks_broadcast,
    build_slip_broadcast,
)
from apps.predictions.models import Market, Prediction, Slip, Tier
from apps.predictions.tests.factories import make_fixture, make_league, make_team

User = get_user_model()


class AudienceTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(
            code="vip", name="VIP", price_usd=Decimal("60"),
            duration_days=30, unlimited=True, includes_vip_slips=True,
        )

    def _user(self, tid, **kwargs):
        return User.objects.create(username=f"tg_{tid}", telegram_id=tid, **kwargs)

    def test_everyone_reachable_is_included_by_default(self):
        self._user(1)
        self._user(2)
        self.assertEqual(audience().count(), 2)

    def test_opted_out_users_are_excluded(self):
        self._user(1)
        self._user(2, notifications_enabled=False)
        self.assertEqual([u.telegram_id for u in audience()], [1])

    def test_blocked_users_are_excluded(self):
        self._user(1)
        self._user(2, is_blocked=True)
        self.assertEqual([u.telegram_id for u in audience()], [1])

    def test_users_without_a_telegram_id_are_excluded(self):
        """Web-only accounts have nowhere to push to."""
        self._user(1)
        User.objects.create(username="web_only", telegram_id=None)
        self.assertEqual(audience().count(), 1)

    def test_vip_targeting_includes_only_current_subscribers(self):
        subscriber = self._user(1)
        Subscription.objects.create(
            user=subscriber, plan=self.plan, expires_at=timezone.now() + timedelta(days=5)
        )
        lapsed = self._user(2)
        Subscription.objects.create(
            user=lapsed, plan=self.plan, expires_at=timezone.now() - timedelta(days=1)
        )
        self._user(3)  # never subscribed

        self.assertEqual([u.telegram_id for u in audience(Tier.VIP)], [1])

    def test_vip_targeting_still_respects_opt_out(self):
        muted = self._user(1, notifications_enabled=False)
        Subscription.objects.create(
            user=muted, plan=self.plan, expires_at=timezone.now() + timedelta(days=5)
        )
        self.assertEqual(audience(Tier.VIP).count(), 0)


class BroadcastContentTests(TestCase):
    def setUp(self):
        self.league = make_league()

    def _pick(self, confidence=80, selection="home"):
        stamp = timezone.now().microsecond + confidence
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"H{stamp}"), away=make_team(f"A{stamp}"),
        )
        return Prediction.objects.create(
            fixture=fixture, market=Market.MATCH_RESULT, selection=selection,
            probability=confidence / 100, confidence=confidence,
            market_odds=Decimal("1.75"), published_at=timezone.now(),
        )

    def test_free_push_shows_the_hook_but_not_the_pick(self):
        """If the push contains the pick, nobody ever spends a credit."""
        picks = [self._pick(85), self._pick(72)]
        broadcast = build_free_picks_broadcast(picks)

        self.assertIn("85%", broadcast.body)
        self.assertIn("Match Result", broadcast.body)

        # For 1X2 the label is a team name, and both teams appear in the fixture
        # line — so naming one reveals nothing. What must not leak is a side being
        # singled out, i.e. each team is mentioned exactly as often as the other.
        for pick in picks:
            home, away = pick.fixture.home.name, pick.fixture.away.name
            self.assertEqual(broadcast.body.count(home), broadcast.body.count(away))

    def test_free_push_hides_selections_that_are_not_team_names(self):
        """BTTS and over/under labels would give the pick away outright."""
        pick = self._pick(80, selection="yes")
        Prediction.objects.filter(pk=pick.pk).update(market=Market.BTTS)
        pick.refresh_from_db()

        body = build_free_picks_broadcast([pick]).body

        self.assertIn("80%", body)
        self.assertNotIn("Yes", body)
        self.assertNotIn(pick.selection_label, body)

    def test_no_broadcast_when_there_are_no_picks(self):
        self.assertIsNone(build_free_picks_broadcast([]))

    def test_slip_push_carries_the_legs_and_targets_vip(self):
        pick = self._pick(88)
        slip = Slip.objects.create(
            kind=Slip.Kind.BANKER, title="Banker of the Day",
            for_date=timezone.now().date(), published_at=timezone.now(),
            total_odds=Decimal("1.75"),
        )
        slip.predictions.set([pick])

        broadcast = build_slip_broadcast(slip)

        self.assertEqual(broadcast.target_tier, Tier.VIP)
        self.assertIn(pick.selection_label, broadcast.body)
        self.assertIn("1.75", broadcast.body)
