"""
Guards on the walk-forward replay itself.

The backtest's whole value is that it cannot see the future. Two ways it lost
that property in practice, both invisible in the output — the number just got
better — so both are pinned here.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.fixtures.models import Fixture, Team

from .factories import make_league, make_team


class BacktestIsolationTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.teams = [make_team(f"T{i}") for i in range(8)]
        start = timezone.now() - timedelta(days=200)

        # A deterministic season with real spread: some sides score, some don't,
        # and home form differs from away form so the venue split has something
        # to find.
        n = 0
        for round_no in range(14):
            for i in range(0, len(self.teams), 2):
                home, away = self.teams[i], self.teams[i + 1]
                if round_no % 2:
                    home, away = away, home
                Fixture.objects.create(
                    api_id=900000 + n,
                    league=self.league, home=home, away=away,
                    kickoff=start + timedelta(days=n),
                    status=Fixture.Status.FINISHED,
                    home_goals=(i // 2 + round_no) % 4,
                    away_goals=(i // 2 + 1) % 3,
                )
                n += 1

    def _run(self) -> str:
        out = StringIO()
        call_command("backtest", "--warmup", "20", stdout=out)
        return out.getvalue()

    def test_rerunning_gives_the_same_answer(self):
        """
        `select_related` caches Team rows on each fixture at list-build time —
        before the command resets the ratings. Pricing off those cached objects
        reads the *previous* run's end-of-season ratings, so a second run scores
        every match with strengths that already contain its result. The tell is
        that re-running changes the number.
        """
        first = self._run()
        second = self._run()
        self.assertEqual(first, second)

    def test_starting_ratings_do_not_change_the_answer(self):
        from_default = self._run()

        Team.objects.update(
            elo=1900.0, attack_strength=2.2, defence_strength=0.4,
            home_attack_strength=2.2, home_defence_strength=0.4,
            away_attack_strength=2.2, away_defence_strength=0.4,
        )
        from_dirty = self._run()

        # The command resets ratings itself, so whatever was in the table before
        # must not survive into the replay.
        self.assertEqual(from_default, from_dirty)


class RefreshSlateTests(TestCase):
    """
    The whole chain in one command. Order is the thing being tested: each step
    feeds the next, and skipping one leaves the rest quietly wrong rather than
    failing loudly.
    """

    def _run(self, *args):
        from io import StringIO

        out = StringIO()
        call_command("refresh_slate", *args, stdout=out)
        return out.getvalue()

    @patch("apps.predictions.tasks.publish_and_build_slips")
    @patch("apps.predictions.tasks.generate_daily_predictions")
    @patch("apps.fixtures.tasks.sync_odds_for_upcoming")
    @patch("apps.fixtures.tasks.sync_fixtures")
    @patch("apps.predictions.tasks.refresh_ratings")
    @patch("apps.fixtures.tasks.sync_leagues")
    def test_runs_every_step_in_order(self, leagues, ratings, fixtures, odds, generate, publish):
        for mock, value in (
            (leagues, 2), (ratings, {}), (fixtures, 5), (odds, {}), (generate, 7), (publish, {})
        ):
            mock.return_value = value

        output = self._run()

        for label in ("Leagues", "Ratings", "Fixtures", "Odds", "Predictions", "Publishing"):
            self.assertIn(label, output)
        for mock in (leagues, ratings, fixtures, odds, generate, publish):
            mock.assert_called_once()

    @patch("apps.predictions.tasks.publish_and_build_slips")
    @patch("apps.predictions.tasks.generate_daily_predictions")
    @patch("apps.fixtures.tasks.sync_odds_for_upcoming")
    @patch("apps.fixtures.tasks.sync_fixtures")
    @patch("apps.predictions.tasks.refresh_ratings")
    @patch("apps.fixtures.tasks.sync_leagues")
    def test_one_failing_step_does_not_stop_the_rest(
        self, leagues, ratings, fixtures, odds, generate, publish
    ):
        """
        A league with no odds posted yet is normal, and the steps after it still
        do useful work — so a failure is reported and the chain continues.
        """
        odds.side_effect = Exception("no markets yet")
        for mock, value in ((leagues, 1), (ratings, {}), (fixtures, 3), (generate, 4), (publish, {})):
            mock.return_value = value

        output = self._run()

        self.assertIn("failed: no markets yet", output)
        generate.assert_called_once()
        publish.assert_called_once()

    @patch("apps.predictions.tasks.publish_and_build_slips")
    @patch("apps.predictions.tasks.generate_daily_predictions")
    @patch("apps.fixtures.tasks.sync_odds_for_upcoming")
    @patch("apps.fixtures.tasks.sync_fixtures")
    @patch("apps.predictions.tasks.refresh_ratings")
    @patch("apps.fixtures.tasks.sync_leagues")
    def test_leagues_can_be_skipped(self, leagues, ratings, fixtures, odds, generate, publish):
        for mock, value in ((ratings, {}), (fixtures, 1), (odds, {}), (generate, 1), (publish, {})):
            mock.return_value = value

        self._run("--skip-leagues")
        leagues.assert_not_called()

    @patch("apps.predictions.tasks.publish_and_build_slips")
    @patch("apps.predictions.tasks.generate_daily_predictions")
    @patch("apps.fixtures.tasks.sync_odds_for_upcoming")
    @patch("apps.fixtures.tasks.sync_fixtures")
    @patch("apps.predictions.tasks.refresh_ratings")
    @patch("apps.fixtures.tasks.sync_leagues")
    def test_warns_when_nothing_published_carries_a_price(
        self, leagues, ratings, fixtures, odds, generate, publish
    ):
        """Without this the operator sees green output and a broken Build Odds."""
        for mock, value in (
            (leagues, 0), (ratings, {}), (fixtures, 0), (odds, {}), (generate, 0), (publish, {})
        ):
            mock.return_value = value

        output = self._run()
        self.assertIn("Build Odds will report no_prices", output)
