"""
Odds-target slip building.

"Give me 3-5 odds" asks for a payout, not a leg count. These pin the three
things that make the answer trustworthy: it lands in range, it picks the
combination most likely to win rather than the one with the best-looking legs,
and it returns nothing rather than padding to reach a number.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.predictions import odds_builder
from apps.predictions.models import Market, Prediction, Tier

from .factories import make_fixture, make_league, make_team


class OddsBuilderTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.today = timezone.now().date()
        self._n = 0

    def _leg(self, odds, confidence, days_ahead=0, market=Market.MATCH_RESULT, fixture=None):
        self._n += 1
        fixture = fixture or make_fixture(
            league=self.league,
            home=make_team(f"H{self._n}"), away=make_team(f"A{self._n}"),
            kickoff=timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
                    + timedelta(days=days_ahead, minutes=self._n),
        )
        return Prediction.objects.create(
            fixture=fixture, market=market, selection="home",
            probability=confidence / 100, confidence=confidence,
            market_odds=Decimal(str(odds)),
            tier=Tier.VIP, published_at=timezone.now(),
        )

    def test_lands_inside_the_requested_range(self):
        for odds in ("1.40", "1.50", "1.60", "1.80"):
            self._leg(odds, 72)

        slip = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        self.assertIsNotNone(slip)
        self.assertGreaterEqual(float(slip.combined_odds), 3.0)
        self.assertLessEqual(float(slip.combined_odds), 5.0)

    def test_prefers_the_combination_most_likely_to_win(self):
        """
        Not the same as picking the best-looking legs: three at 85% beat two at
        88% once the probabilities multiply, and the payout is what is fixed.
        """
        # Two routes to roughly the same payout.
        self._leg("2.00", 55)   # long-ish, low probability
        self._leg("2.10", 55)
        self._leg("1.60", 85)   # short, high probability
        self._leg("1.55", 85)
        self._leg("1.70", 85)

        slip = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0,
                                             min_confidence=50)

        self.assertIsNotNone(slip)
        # The 55% pair multiplies to ~0.30; the 85% trio to ~0.61.
        self.assertGreater(slip.combined_probability, 0.5)
        self.assertTrue(all(leg.confidence >= 85 for leg in slip.legs))

    def test_returns_nothing_rather_than_padding_to_reach_the_target(self):
        """A thin slate legitimately produces no slip. Inventing one ruins the record."""
        self._leg("1.10", 90)
        self._leg("1.12", 90)

        slip = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)
        self.assertIsNone(slip)

    def test_never_puts_two_legs_on_the_same_fixture(self):
        """Correlated legs make the combined odds lie about the real risk."""
        shared = make_fixture(
            league=self.league, home=make_team("Same"), away=make_team("Match"),
            kickoff=timezone.now().replace(hour=12, minute=0),
        )
        self._leg("1.90", 75, market=Market.MATCH_RESULT, fixture=shared)
        self._leg("1.85", 75, market=Market.OVER_UNDER_25, fixture=shared)
        self._leg("1.70", 75)

        slip = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        if slip:
            fixtures = [leg.fixture_id for leg in slip.legs]
            self.assertEqual(len(fixtures), len(set(fixtures)))

    def test_spans_a_date_range_for_weekend_requests(self):
        self._leg("1.80", 80, days_ahead=0)
        self._leg("1.90", 80, days_ahead=1)
        self._leg("1.70", 80, days_ahead=2)

        same_day = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)
        weekend = odds_builder.build_for_target(
            self.today, self.today + timedelta(days=2), min_odds=3.0, max_odds=5.0
        )

        self.assertIsNone(same_day, "one leg cannot reach 3.00")
        self.assertIsNotNone(weekend)
        self.assertGreaterEqual(len(weekend.legs), 2)

    def test_unpublished_picks_are_never_used(self):
        """Tier defaults to FREE on unpublished rows, so published_at is the real gate."""
        for odds in ("1.60", "1.70", "1.80"):
            leg = self._leg(odds, 80)
            leg.published_at = None
            leg.save()

        self.assertIsNone(odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0))

    def test_legs_without_a_price_cannot_be_used(self):
        for odds in ("1.60", "1.70", "1.80"):
            leg = self._leg(odds, 80)
            leg.market_odds = None
            leg.save()

        self.assertIsNone(odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0))

    def test_legs_are_ordered_by_kickoff(self):
        self._leg("1.60", 80, days_ahead=2)
        self._leg("1.70", 80, days_ahead=0)
        self._leg("1.80", 80, days_ahead=1)

        slip = odds_builder.build_for_target(
            self.today, self.today + timedelta(days=2), min_odds=3.0, max_odds=5.0
        )
        kickoffs = [leg.fixture.kickoff for leg in slip.legs]
        self.assertEqual(kickoffs, sorted(kickoffs))

    def test_a_nonsensical_range_is_rejected(self):
        with self.assertRaises(ValueError):
            odds_builder.build_for_target(self.today, min_odds=5.0, max_odds=3.0)
        with self.assertRaises(ValueError):
            odds_builder.build_for_target(self.today, min_odds=0.5, max_odds=5.0)
