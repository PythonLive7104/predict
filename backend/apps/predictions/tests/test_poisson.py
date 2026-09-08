from django.test import SimpleTestCase

from apps.predictions.engine import poisson


class ScoreMatrixTests(SimpleTestCase):
    def test_matrix_is_a_probability_distribution(self):
        """The Dixon-Coles correction breaks normalisation; we renormalise after."""
        matrix = poisson.score_matrix(1.6, 1.1)
        self.assertAlmostEqual(matrix.sum(), 1.0, places=9)
        self.assertTrue((matrix >= 0).all())

    def test_dixon_coles_lifts_low_scoring_draws(self):
        """Plain Poisson under-counts 0-0 and 1-1 — that's the whole point of tau."""
        from scipy.stats import poisson as sp

        lam_h, lam_a = 1.1, 0.9
        corrected = poisson.score_matrix(lam_h, lam_a)
        plain_00 = sp.pmf(0, lam_h) * sp.pmf(0, lam_a)
        plain_11 = sp.pmf(1, lam_h) * sp.pmf(1, lam_a)

        self.assertGreater(corrected[0, 0], plain_00)
        self.assertGreater(corrected[1, 1], plain_11)


class MarketProbabilityTests(SimpleTestCase):
    def test_outcomes_partition_probability_space(self):
        probs = poisson.market_probabilities(1.5, 1.2)
        self.assertAlmostEqual(probs.home + probs.draw + probs.away, 1.0, places=6)
        self.assertAlmostEqual(probs.over_2_5 + probs.under_2_5, 1.0, places=6)
        self.assertAlmostEqual(probs.btts_yes + probs.btts_no, 1.0, places=6)

    def test_stronger_attack_raises_home_probability(self):
        weak = poisson.market_probabilities(1.0, 1.0)
        strong = poisson.market_probabilities(2.4, 1.0)
        self.assertGreater(strong.home, weak.home)
        self.assertLess(strong.away, weak.away)

    def test_equal_lambdas_are_symmetric(self):
        probs = poisson.market_probabilities(1.3, 1.3)
        self.assertAlmostEqual(probs.home, probs.away, places=9)


class ExpectedGoalsTests(SimpleTestCase):
    def test_home_advantage_favours_the_home_side(self):
        lam_h, lam_a = poisson.expected_goals(1.0, 1.0, 1.0, 1.0)
        self.assertGreater(lam_h, lam_a)

    def test_extreme_strengths_are_clamped(self):
        """A runaway ratio must not produce a six-goal expectation."""
        lam_h, _ = poisson.expected_goals(50.0, 1.0, 1.0, 50.0)
        self.assertLessEqual(lam_h, 5.0)


class PricingTests(SimpleTestCase):
    def test_fair_odds_invert_probability(self):
        self.assertAlmostEqual(poisson.fair_odds(0.5), 2.0)
        self.assertAlmostEqual(poisson.fair_odds(0.25), 4.0)

    def test_edge_sign_reflects_value(self):
        # Book pays 2.5 on a coin flip -> positive edge.
        self.assertGreater(poisson.edge(2.5, 0.5), 0)
        # Book pays 1.5 on a coin flip -> negative edge.
        self.assertLess(poisson.edge(1.5, 0.5), 0)

    def test_elo_probability_is_monotonic_in_rating_gap(self):
        self.assertGreater(
            poisson.elo_win_probability(1900, 1500), poisson.elo_win_probability(1600, 1500)
        )


class DoubleChanceSelectionTests(SimpleTestCase):
    """
    The 12 market ("home or away") wins whenever the draw is least likely, which
    happens constantly for heavy favourites — but it is a bet against a draw, not
    a safer way to back the favourite. It must never be offered as the safety pick.
    """

    def _dc(self, lam_home, lam_away):
        from apps.predictions.engine.pipeline import candidates_from
        from apps.predictions.models import Market

        probs = poisson.market_probabilities(lam_home, lam_away)
        return next(c for c in candidates_from(probs) if c.market == Market.DOUBLE_CHANCE)

    def test_heavy_home_favourite_gets_home_or_draw(self):
        self.assertEqual(self._dc(2.9, 0.8).selection, "home_draw")

    def test_heavy_away_favourite_gets_draw_or_away(self):
        self.assertEqual(self._dc(0.8, 2.9).selection, "draw_away")

    def test_never_offers_the_no_draw_pair(self):
        for lam_home, lam_away in [(2.9, 0.8), (0.8, 2.9), (1.4, 1.4), (2.2, 1.9)]:
            with self.subTest(lambdas=(lam_home, lam_away)):
                self.assertNotEqual(self._dc(lam_home, lam_away).selection, "home_away")

    def test_double_chance_probability_matches_its_two_outcomes(self):
        probs = poisson.market_probabilities(2.9, 0.8)
        candidate = self._dc(2.9, 0.8)
        self.assertAlmostEqual(candidate.probability, probs.home + probs.draw, places=9)


class VenueModeTests(SimpleTestCase):
    """
    Venue strengths are normalised against venue-specific league averages, so the
    home edge is already inside the baseline. Applying `home_advantage` on top
    would count it twice.
    """

    def test_average_sides_get_exactly_the_league_venue_averages(self):
        lam_h, lam_a = poisson.expected_goals(
            1.0, 1.0, 1.0, 1.0, league_home_avg=1.52, league_away_avg=1.18,
        )
        self.assertAlmostEqual(lam_h, 1.52, places=6)
        self.assertAlmostEqual(lam_a, 1.18, places=6)

    def test_home_advantage_multiplier_is_ignored_in_venue_mode(self):
        without = poisson.expected_goals(
            1.0, 1.0, 1.0, 1.0, league_home_avg=1.5, league_away_avg=1.2,
        )
        with_flag = poisson.expected_goals(
            1.0, 1.0, 1.0, 1.0, home_advantage=1.9,
            league_home_avg=1.5, league_away_avg=1.2,
        )
        self.assertEqual(without, with_flag)

    def test_symmetric_mode_is_unchanged_when_only_one_average_is_given(self):
        """A half-specified call must not silently drop the home advantage."""
        partial = poisson.expected_goals(1.0, 1.0, 1.0, 1.0, league_home_avg=1.5)
        legacy = poisson.expected_goals(1.0, 1.0, 1.0, 1.0)
        self.assertEqual(partial, legacy)
        self.assertGreater(legacy[0], legacy[1])

    def test_venue_strengths_move_the_lambdas_the_right_way(self):
        # A strong home attack against a leaky away defence.
        strong, _ = poisson.expected_goals(
            1.4, 1.0, 1.0, 1.3, league_home_avg=1.5, league_away_avg=1.2,
        )
        base, _ = poisson.expected_goals(
            1.0, 1.0, 1.0, 1.0, league_home_avg=1.5, league_away_avg=1.2,
        )
        self.assertGreater(strong, base)
