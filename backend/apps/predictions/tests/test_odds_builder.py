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

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        self.assertTrue(result.found, result.reason)
        slip = result.slip
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

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0,
                                             min_confidence=50)

        self.assertTrue(result.found, result.reason)
        slip = result.slip
        # The 55% pair multiplies to ~0.30; the 85% trio to ~0.61.
        self.assertGreater(slip.combined_probability, 0.5)
        self.assertTrue(all(leg.confidence >= 85 for leg in slip.legs))

    def test_returns_nothing_rather_than_padding_to_reach_the_target(self):
        """A thin slate legitimately produces no slip. Inventing one ruins the record."""
        self._leg("1.10", 90)
        self._leg("1.12", 90)

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)
        self.assertFalse(result.found)

    def test_never_puts_two_legs_on_the_same_fixture(self):
        """Correlated legs make the combined odds lie about the real risk."""
        shared = make_fixture(
            league=self.league, home=make_team("Same"), away=make_team("Match"),
            kickoff=timezone.now().replace(hour=12, minute=0),
        )
        self._leg("1.90", 75, market=Market.MATCH_RESULT, fixture=shared)
        self._leg("1.85", 75, market=Market.OVER_UNDER_25, fixture=shared)
        self._leg("1.70", 75)

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        if result.found:
            fixtures = [leg.fixture_id for leg in result.slip.legs]
            self.assertEqual(len(fixtures), len(set(fixtures)))

    def test_spans_a_date_range_for_weekend_requests(self):
        self._leg("1.80", 80, days_ahead=0)
        self._leg("1.90", 80, days_ahead=1)
        self._leg("1.70", 80, days_ahead=2)

        same_day = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)
        weekend = odds_builder.build_for_target(
            self.today, self.today + timedelta(days=2), min_odds=3.0, max_odds=5.0
        )

        self.assertFalse(same_day.found, "one leg cannot reach 3.00")
        self.assertTrue(weekend.found, weekend.reason)
        self.assertGreaterEqual(len(weekend.slip.legs), 2)

    def test_unpublished_picks_are_never_used(self):
        """Tier defaults to FREE on unpublished rows, so published_at is the real gate."""
        for odds in ("1.60", "1.70", "1.80"):
            leg = self._leg(odds, 80)
            leg.published_at = None
            leg.save()

        self.assertFalse(odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0).found)

    def test_legs_without_a_price_cannot_be_used(self):
        for odds in ("1.60", "1.70", "1.80"):
            leg = self._leg(odds, 80)
            leg.market_odds = None
            leg.save()

        self.assertFalse(odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0).found)

    def test_legs_are_ordered_by_kickoff(self):
        self._leg("1.60", 80, days_ahead=2)
        self._leg("1.70", 80, days_ahead=0)
        self._leg("1.80", 80, days_ahead=1)

        result = odds_builder.build_for_target(
            self.today, self.today + timedelta(days=2), min_odds=3.0, max_odds=5.0
        )
        kickoffs = [leg.fixture.kickoff for leg in result.slip.legs]
        self.assertEqual(kickoffs, sorted(kickoffs))

    def test_a_nonsensical_range_is_rejected(self):
        with self.assertRaises(ValueError):
            odds_builder.build_for_target(self.today, min_odds=5.0, max_odds=3.0)
        with self.assertRaises(ValueError):
            odds_builder.build_for_target(self.today, min_odds=0.5, max_odds=5.0)


class FailureReasonTests(OddsBuilderTests):
    """
    Three very different situations used to produce one message telling the user
    to "try a lower target". Only one of them is something they can act on, and
    for the other two that advice sends them round a loop that cannot succeed.
    """

    def test_no_prices_at_all_is_reported_as_ours_to_fix(self):
        """
        The state the live bot was actually in: picks published, none priced,
        because nothing fetched odds. Lowering the target would never have helped.
        """
        leg = self._leg("1.80", 80)
        leg.market_odds = None
        leg.save()

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        self.assertEqual(result.reason, "no_prices")
        self.assertEqual(result.priced_fixtures, 0)

    def test_a_single_priced_match_says_so(self):
        self._leg("1.80", 80)

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        self.assertEqual(result.reason, "too_few_matches")
        self.assertEqual(result.priced_fixtures, 1)

    def test_an_unreachable_target_reports_what_is_reachable(self):
        """The one actionable case — so it names the number to aim at."""
        self._leg("1.10", 90)
        self._leg("1.12", 90)
        self._leg("1.15", 90)

        result = odds_builder.build_for_target(self.today, min_odds=5.0, max_odds=10.0)

        self.assertEqual(result.reason, "unreachable")
        self.assertIsNotNone(result.best_available)
        # Three short legs multiply to about 1.42 — well under the 5.0 asked for.
        self.assertLess(float(result.best_available), 5.0)
        self.assertGreater(float(result.best_available), 1.0)

    def test_a_reachable_target_reports_ok(self):
        self._leg("1.80", 80)
        self._leg("1.90", 80)

        result = odds_builder.build_for_target(self.today, min_odds=3.0, max_odds=5.0)

        self.assertEqual(result.reason, "ok")
        self.assertTrue(result.found)
        self.assertEqual(result.priced_fixtures, 2)
