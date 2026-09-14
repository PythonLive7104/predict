from django.test import TestCase

from apps.fixtures.models import Fixture
from apps.predictions.access import record_by_market, record_summary
from apps.predictions.models import Market, Outcome, Prediction

from .factories import make_fixture, make_league, make_team


class RecordByMarketTests(TestCase):
    """
    Prediction has Meta.ordering, which silently breaks values_list().distinct().
    These cover the grouping itself, not just the arithmetic.
    """

    def setUp(self):
        self.league = make_league()

    def _settled(self, market, outcome, selection="home"):
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"H{id(outcome)}{market}{outcome}"),
            away=make_team(f"A{id(outcome)}{market}{outcome}"),
            status=Fixture.Status.FINISHED, home_goals=1, away_goals=0,
        )
        return Prediction.objects.create(
            fixture=fixture, market=market, selection=selection,
            probability=0.7, confidence=70, outcome=outcome,
            published_at=fixture.kickoff,
        )

    def test_each_market_appears_exactly_once(self):
        for _ in range(4):
            self._settled(Market.MATCH_RESULT, Outcome.WON)
        for _ in range(3):
            self._settled(Market.DOUBLE_CHANCE, Outcome.WON)
        self._settled(Market.DOUBLE_CHANCE, Outcome.LOST)

        rows = record_by_market()
        markets = [r["market"] for r in rows]

        self.assertEqual(len(markets), len(set(markets)), f"duplicated rows: {markets}")
        self.assertEqual(len(rows), 2)

    def test_counts_and_rates_are_per_market(self):
        for _ in range(3):
            self._settled(Market.MATCH_RESULT, Outcome.WON)
        self._settled(Market.MATCH_RESULT, Outcome.LOST)
        self._settled(Market.BTTS, Outcome.LOST, selection="yes")

        rows = {r["market"]: r for r in record_by_market()}

        self.assertEqual(rows[Market.MATCH_RESULT]["total"], 4)
        self.assertEqual(rows[Market.MATCH_RESULT]["won"], 3)
        self.assertEqual(rows[Market.MATCH_RESULT]["win_rate"], 75.0)
        self.assertEqual(rows[Market.BTTS]["win_rate"], 0.0)

    def test_ordered_by_volume(self):
        for _ in range(5):
            self._settled(Market.OVER_UNDER_25, Outcome.WON, selection="over")
        self._settled(Market.MATCH_RESULT, Outcome.WON)

        rows = record_by_market()
        self.assertEqual(rows[0]["market"], Market.OVER_UNDER_25)

    def test_pending_and_void_are_excluded(self):
        self._settled(Market.MATCH_RESULT, Outcome.WON)
        self._settled(Market.MATCH_RESULT, Outcome.PENDING)
        self._settled(Market.MATCH_RESULT, Outcome.VOID)

        rows = record_by_market()
        self.assertEqual(rows[0]["total"], 1)
        self.assertEqual(record_summary()["settled"], 1)

    def test_empty_record_returns_no_rows(self):
        self.assertEqual(record_by_market(), [])
        self.assertEqual(record_summary()["settled"], 0)
        self.assertIsNone(record_summary()["roi"])


class SelectionLabelTests(TestCase):
    """One definition, shared by the bot, the API and the LLM prompt."""

    def test_double_chance_resolves_to_club_names(self):
        from apps.predictions.models import selection_label_for

        self.assertEqual(
            selection_label_for("dc", "home_draw", "Man City", "Burnley"),
            "Man City or Draw",
        )

    def test_non_team_markets_use_the_plain_label(self):
        from apps.predictions.models import selection_label_for

        self.assertEqual(
            selection_label_for("ou_2_5", "over", "Man City", "Burnley"),
            "Over 2.5 goals",
        )

    def test_the_model_property_and_the_helper_agree(self):
        """A second copy of this mapping is how the two surfaces drift apart."""
        from apps.predictions.models import Market, Prediction, selection_label_for
        from .factories import make_fixture, make_team

        fixture = make_fixture(home=make_team("Man City"), away=make_team("Burnley"))
        prediction = Prediction.objects.create(
            fixture=fixture, market=Market.DOUBLE_CHANCE, selection="home_draw",
            probability=0.86, confidence=86,
        )
        self.assertEqual(
            prediction.selection_label,
            selection_label_for("dc", "home_draw", "Man City", "Burnley"),
        )


class AdminEntitlementTests(TestCase):
    """
    The owner should not have to buy their own product. Anyone who can reach the
    Django admin already reads every prediction there, so gating the bot against
    them protects nothing.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.User = get_user_model()

    def _user(self, **flags):
        from apps.billing.models import Wallet

        user = self.User.objects.create(username=f"u{self.User.objects.count()}", **flags)
        Wallet.objects.create(user=user)
        return user

    def test_a_superuser_has_unlimited_vip_and_no_expiry(self):
        from apps.predictions.access import entitlement_for

        e = entitlement_for(self._user(is_superuser=True, is_staff=True))

        self.assertTrue(e.unlimited)
        self.assertTrue(e.vip)
        self.assertIsNone(e.expires_at, "an admin's access is not a lapsing subscription")
        self.assertEqual(e.plan_name, "Admin")

    def test_staff_without_superuser_also_qualify(self):
        from apps.predictions.access import entitlement_for

        self.assertTrue(entitlement_for(self._user(is_staff=True)).unlimited)

    def test_a_superuser_who_is_not_staff_still_qualifies(self):
        """The flags are independent; checking only one locks out the owner."""
        from apps.predictions.access import entitlement_for

        self.assertTrue(entitlement_for(self._user(is_superuser=True)).unlimited)

    def test_an_ordinary_user_is_unaffected(self):
        from apps.predictions.access import entitlement_for

        e = entitlement_for(self._user())

        self.assertFalse(e.unlimited)
        self.assertFalse(e.vip)
        self.assertEqual(e.plan_name, "Free")

    def test_an_admin_can_view_any_pick_without_unlocking(self):
        from apps.predictions.access import can_view
        from apps.predictions.models import Market, Prediction

        from .factories import make_fixture

        pick = Prediction.objects.create(
            fixture=make_fixture(), market=Market.MATCH_RESULT, selection="home",
            probability=0.7, confidence=70,
        )
        self.assertTrue(can_view(self._user(is_superuser=True), pick))
        self.assertFalse(can_view(self._user(), pick))

    def test_admins_are_in_the_vip_broadcast_audience(self):
        """
        audience() walks the same shared rule, so the staff carve-out reaches it
        automatically — a hand-written .filter() on subscriptions would not.
        """
        from apps.bot.notifications import audience
        from apps.predictions.models import Tier

        admin = self._user(is_superuser=True)
        admin.telegram_id = 9001
        admin.save()
        plain = self._user()
        plain.telegram_id = 9002
        plain.save()

        vip = list(audience(Tier.VIP))
        self.assertIn(admin, vip)
        self.assertNotIn(plain, vip)
