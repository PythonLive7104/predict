"""
Guards on the walk-forward replay itself.

The backtest's whole value is that it cannot see the future. Two ways it lost
that property in practice, both invisible in the output — the number just got
better — so both are pinned here.
"""

from datetime import timedelta
from io import StringIO

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
