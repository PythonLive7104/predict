from django.test import TestCase

from apps.fixtures.models import Fixture
from apps.predictions.models import Market, Outcome, Prediction
from apps.predictions.tasks import grade, settle_predictions

from .factories import make_fixture


class GradingTests(TestCase):
    def _graded(self, market, selection, home_goals, away_goals):
        fixture = make_fixture(
            status=Fixture.Status.FINISHED, home_goals=home_goals, away_goals=away_goals
        )
        prediction = Prediction.objects.create(
            fixture=fixture, market=market, selection=selection,
            probability=0.6, confidence=60,
        )
        return grade(prediction)

    def test_match_result(self):
        self.assertEqual(self._graded(Market.MATCH_RESULT, "home", 2, 0), Outcome.WON)
        self.assertEqual(self._graded(Market.MATCH_RESULT, "home", 0, 2), Outcome.LOST)
        self.assertEqual(self._graded(Market.MATCH_RESULT, "draw", 1, 1), Outcome.WON)
        self.assertEqual(self._graded(Market.MATCH_RESULT, "away", 0, 1), Outcome.WON)

    def test_over_under_line_is_exclusive(self):
        # Exactly 2 goals is under 2.5, 3 goals is over. The .5 line never pushes.
        self.assertEqual(self._graded(Market.OVER_UNDER_25, "over", 1, 1), Outcome.LOST)
        self.assertEqual(self._graded(Market.OVER_UNDER_25, "over", 2, 1), Outcome.WON)
        self.assertEqual(self._graded(Market.OVER_UNDER_25, "under", 1, 1), Outcome.WON)

    def test_btts_requires_both_sides_to_score(self):
        self.assertEqual(self._graded(Market.BTTS, "yes", 1, 1), Outcome.WON)
        self.assertEqual(self._graded(Market.BTTS, "yes", 3, 0), Outcome.LOST)
        self.assertEqual(self._graded(Market.BTTS, "no", 3, 0), Outcome.WON)

    def test_double_chance_covers_two_outcomes(self):
        self.assertEqual(self._graded(Market.DOUBLE_CHANCE, "home_draw", 1, 1), Outcome.WON)
        self.assertEqual(self._graded(Market.DOUBLE_CHANCE, "home_draw", 2, 0), Outcome.WON)
        self.assertEqual(self._graded(Market.DOUBLE_CHANCE, "home_draw", 0, 2), Outcome.LOST)
        self.assertEqual(self._graded(Market.DOUBLE_CHANCE, "draw_away", 0, 2), Outcome.WON)

    def test_correct_score(self):
        self.assertEqual(self._graded(Market.CORRECT_SCORE, "2-1", 2, 1), Outcome.WON)
        self.assertEqual(self._graded(Market.CORRECT_SCORE, "2-1", 1, 2), Outcome.LOST)

    def test_unfinished_fixture_is_not_gradeable(self):
        fixture = make_fixture()  # still scheduled
        prediction = Prediction.objects.create(
            fixture=fixture, market=Market.MATCH_RESULT, selection="home",
            probability=0.6, confidence=60,
        )
        self.assertIsNone(grade(prediction))


class SettlementTests(TestCase):
    def test_settlement_is_idempotent(self):
        """A second run must not re-settle, or Elo double-counts the result."""
        fixture = make_fixture(status=Fixture.Status.FINISHED, home_goals=2, away_goals=0)
        Prediction.objects.create(
            fixture=fixture, market=Market.MATCH_RESULT, selection="home",
            probability=0.6, confidence=60, published_at="2026-01-01T00:00:00Z",
        )

        self.assertEqual(settle_predictions(), 1)
        self.assertEqual(settle_predictions(), 0)

        fixture.refresh_from_db()
        self.assertTrue(fixture.elo_applied)
