from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.fixtures.models import Fixture
from apps.predictions.engine import ratings

from .factories import make_fixture, make_league, make_team


class EloTests(TestCase):
    def test_beating_a_stronger_side_gains_more_than_beating_a_weaker_one(self):
        league = make_league()

        underdog = make_team("Underdog", elo=1500)
        favourite = make_team("Favourite", elo=1900)
        upset = make_fixture(
            league=league, home=underdog, away=favourite,
            status=Fixture.Status.FINISHED, home_goals=1, away_goals=0,
        )
        ratings.apply_elo(upset)
        underdog.refresh_from_db()
        upset_gain = underdog.elo - 1500

        strong = make_team("Strong", elo=1900)
        weak = make_team("Weak", elo=1500)
        expected_win = make_fixture(
            league=league, home=strong, away=weak,
            status=Fixture.Status.FINISHED, home_goals=1, away_goals=0,
        )
        ratings.apply_elo(expected_win)
        strong.refresh_from_db()
        routine_gain = strong.elo - 1900

        self.assertGreater(upset_gain, routine_gain)

    def test_elo_is_zero_sum(self):
        home, away = make_team("H", elo=1600), make_team("A", elo=1600)
        fixture = make_fixture(
            home=home, away=away, status=Fixture.Status.FINISHED,
            home_goals=3, away_goals=1,
        )
        ratings.apply_elo(fixture)
        home.refresh_from_db()
        away.refresh_from_db()
        self.assertAlmostEqual(home.elo + away.elo, 3200.0, places=6)

    def test_bigger_margin_moves_rating_further_but_sublinearly(self):
        def gain(hg, ag):
            home, away = make_team("H", elo=1600), make_team("A", elo=1600)
            fx = make_fixture(
                home=home, away=away, status=Fixture.Status.FINISHED,
                home_goals=hg, away_goals=ag,
            )
            ratings.apply_elo(fx)
            home.refresh_from_db()
            return home.elo - 1600

        narrow, thrashing = gain(1, 0), gain(4, 0)
        self.assertGreater(thrashing, narrow)
        # Dampened: a 4-goal win is worth well under four 1-goal wins.
        self.assertLess(thrashing, narrow * 4)

    def test_applying_twice_is_a_no_op(self):
        home, away = make_team("H", elo=1600), make_team("A", elo=1600)
        fixture = make_fixture(
            home=home, away=away, status=Fixture.Status.FINISHED,
            home_goals=2, away_goals=0,
        )
        ratings.apply_elo(fixture)
        home.refresh_from_db()
        after_first = home.elo

        fixture.refresh_from_db()
        ratings.apply_elo(fixture)
        home.refresh_from_db()
        self.assertEqual(home.elo, after_first)

    def test_unsettled_fixture_is_ignored(self):
        home = make_team("H", elo=1600)
        fixture = make_fixture(home=home)  # scheduled, no goals
        ratings.apply_elo(fixture)
        home.refresh_from_db()
        self.assertEqual(home.elo, 1600)


class StrengthTests(TestCase):
    def _play(self, league, home, away, hg, ag, days_ago=10):
        return Fixture.objects.create(
            api_id=abs(hash((home.pk, away.pk, days_ago, hg, ag))) % 10**8,
            league=league, home=home, away=away,
            kickoff=timezone.now() - timedelta(days=days_ago),
            status=Fixture.Status.FINISHED, home_goals=hg, away_goals=ag,
        )

    def test_prolific_team_gets_a_higher_attack_rating(self):
        league = make_league()
        scorers = make_team("Scorers")
        blunt = make_team("Blunt")
        filler = [make_team(f"Filler{i}") for i in range(6)]

        for i, opponent in enumerate(filler):
            self._play(league, scorers, opponent, 4, 0, days_ago=7 + i)
            self._play(league, blunt, opponent, 0, 1, days_ago=7 + i)

        ratings.update_strengths(league)
        scorers.refresh_from_db()
        blunt.refresh_from_db()

        self.assertGreater(scorers.attack_strength, blunt.attack_strength)
        self.assertLess(scorers.defence_strength, blunt.defence_strength)

    def test_small_samples_are_shrunk_toward_the_league_average(self):
        """One 6-0 must not make a team look like it scores six a game."""
        league = make_league()
        lucky = make_team("Lucky")
        victim = make_team("Victim")
        self._play(league, lucky, victim, 6, 0, days_ago=3)

        ratings.update_strengths(league)
        lucky.refresh_from_db()

        # Raw ratio would be ~4.4x the league average; shrinkage must pull it well
        # below that, and the clamp caps it regardless.
        self.assertLess(lucky.attack_strength, 2.5)

    def test_strengths_stay_within_clamp(self):
        league = make_league()
        monster = make_team("Monster")
        for i in range(10):
            self._play(league, monster, make_team(f"Opp{i}"), 9, 0, days_ago=5 + i)

        ratings.update_strengths(league)
        monster.refresh_from_db()

        self.assertLessEqual(monster.attack_strength, ratings.MAX_STRENGTH)
        self.assertGreaterEqual(monster.defence_strength, ratings.MIN_STRENGTH)

    def test_recent_results_outweigh_stale_ones(self):
        league = make_league()
        improved = make_team("Improved")
        opponents = [make_team(f"O{i}") for i in range(8)]

        # Terrible a year ago, excellent this month.
        for i, opponent in enumerate(opponents[:4]):
            self._play(league, improved, opponent, 0, 3, days_ago=330 + i)
        for i, opponent in enumerate(opponents[4:]):
            self._play(league, improved, opponent, 3, 0, days_ago=5 + i)

        ratings.update_strengths(league)
        improved.refresh_from_db()

        # A flat average would land near 1.0; decay should put attack above it.
        self.assertGreater(improved.attack_strength, 1.0)


class VenueSplitTests(TestCase):
    """
    The split exists for one fixture type: the side whose home and away form are
    nothing alike. A single rating averages those into mush.
    """

    def _play(self, league, home, away, hg, ag, days_ago=10):
        return Fixture.objects.create(
            api_id=abs(hash((home.pk, away.pk, days_ago, hg, ag))) % 10**8,
            league=league, home=home, away=away,
            kickoff=timezone.now() - timedelta(days=days_ago),
            status=Fixture.Status.FINISHED, home_goals=hg, away_goals=ag,
        )

    def test_fortress_at_home_and_dreadful_away_is_not_averaged_into_mush(self):
        league = make_league()
        jekyll = make_team("Jekyll")
        opponents = [make_team(f"Opp{i}") for i in range(12)]

        # Scores freely and concedes nothing at home; the reverse on the road.
        for i, opponent in enumerate(opponents[:6]):
            self._play(league, jekyll, opponent, 3, 0, days_ago=10 + i)
        for i, opponent in enumerate(opponents[6:]):
            self._play(league, opponent, jekyll, 3, 0, days_ago=10 + i)

        ratings.update_strengths(league)
        jekyll.refresh_from_db()

        self.assertGreater(jekyll.home_attack_strength, jekyll.away_attack_strength)
        # Defence is "goals conceded", so lower is better: solid at home, leaky away.
        self.assertLess(jekyll.home_defence_strength, jekyll.away_defence_strength)

        # The venue-agnostic rating sits between the two, which is exactly the
        # information the split recovers.
        self.assertLess(jekyll.home_attack_strength, jekyll.attack_strength * 3)
        self.assertGreater(jekyll.attack_strength, jekyll.away_attack_strength)

    def test_thin_venue_sample_is_shrunk_toward_the_team_not_the_league(self):
        """
        A strong side with one bad home game must not be rated as an average
        home team — the team's own overall form is the better prior.
        """
        league = make_league()
        strong = make_team("Strong")
        opponents = [make_team(f"O{i}") for i in range(9)]

        for i, opponent in enumerate(opponents[:8]):
            self._play(league, opponent, strong, 0, 3, days_ago=10 + i)
        # Exactly one home game, and a blank in it.
        self._play(league, strong, opponents[8], 0, 0, days_ago=5)

        ratings.update_strengths(league)
        strong.refresh_from_db()

        # Raw home ratio is 0.0. Shrinkage must pull it up toward the (high)
        # overall attack without reaching it.
        self.assertGreater(strong.home_attack_strength, 0.0)
        self.assertLess(strong.home_attack_strength, strong.attack_strength)

    def test_venue_strengths_respect_the_clamp(self):
        league = make_league()
        monster = make_team("Monster")
        for i in range(10):
            self._play(league, monster, make_team(f"Opp{i}"), 9, 0, days_ago=5 + i)

        ratings.update_strengths(league)
        monster.refresh_from_db()

        for field in (
            "home_attack_strength", "home_defence_strength",
            "away_attack_strength", "away_defence_strength",
        ):
            value = getattr(monster, field)
            self.assertLessEqual(value, ratings.MAX_STRENGTH, field)
            self.assertGreaterEqual(value, ratings.MIN_STRENGTH, field)

    def test_home_advantage_is_measured_from_the_league(self):
        league = make_league()
        teams = [make_team(f"T{i}") for i in range(8)]
        # Home sides win 2-1 across the board: a real, if crude, home edge.
        for i in range(0, len(teams), 2):
            for day in range(6):
                self._play(league, teams[i], teams[i + 1], 2, 1, days_ago=10 + day)

        home_avg, away_avg = ratings.league_venue_averages(league)
        self.assertAlmostEqual(home_avg, 2.0, places=6)
        self.assertAlmostEqual(away_avg, 1.0, places=6)

    def test_falls_back_to_defaults_when_the_league_is_too_thin(self):
        league = make_league()
        home_avg, away_avg = ratings.league_venue_averages(league)
        self.assertEqual((home_avg, away_avg), (ratings.DEFAULT_HOME_GOALS, ratings.DEFAULT_AWAY_GOALS))


class LookaheadTests(TestCase):
    """
    `as_of` is what makes the walk-forward backtest honest. During a replay every
    fixture in the table is already FINISHED, so an unbounded refresh would rate
    a match using results that had not happened yet.
    """

    def _play(self, league, home, away, hg, ag, days_ago):
        return Fixture.objects.create(
            api_id=abs(hash((home.pk, away.pk, days_ago, hg, ag))) % 10**8,
            league=league, home=home, away=away,
            kickoff=timezone.now() - timedelta(days=days_ago),
            status=Fixture.Status.FINISHED, home_goals=hg, away_goals=ag,
        )

    def test_results_after_the_cutoff_do_not_inform_the_rating(self):
        league = make_league()
        team = make_team("Late Bloomer")
        opponents = [make_team(f"O{i}") for i in range(10)]

        # Poor early, prolific later.
        for i, opponent in enumerate(opponents[:5]):
            self._play(league, team, opponent, 0, 2, days_ago=100 + i)
        for i, opponent in enumerate(opponents[5:]):
            self._play(league, team, opponent, 5, 0, days_ago=10 + i)

        cutoff = timezone.now() - timedelta(days=50)
        ratings.update_strengths(league, as_of=cutoff)
        team.refresh_from_db()
        blind = team.attack_strength

        ratings.update_strengths(league)
        team.refresh_from_db()

        # Knowing about the goal glut must change the rating; not knowing must
        # leave the team looking as poor as its early results.
        self.assertLess(blind, 1.0)
        self.assertGreater(team.attack_strength, blind)

    def test_cutoff_also_bounds_the_league_averages(self):
        league = make_league()
        teams = [make_team(f"T{i}") for i in range(4)]
        # Both batches must clear MIN_FIXTURES_FOR_LEAGUE_AVG on their own,
        # otherwise the bounded call just falls back to the default and the
        # assertion proves nothing about the cutoff.
        for day in range(22):
            self._play(league, teams[0], teams[1], 1, 1, days_ago=100 + day)
        for day in range(22):
            self._play(league, teams[2], teams[3], 4, 4, days_ago=10 + day)

        early = ratings.league_average_goals(league, as_of=timezone.now() - timedelta(days=50))
        full = ratings.league_average_goals(league)
        self.assertAlmostEqual(early, 1.0, places=6)
        self.assertGreater(full, early)
