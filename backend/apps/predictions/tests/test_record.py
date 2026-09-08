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
