"""
Statistical layer — a Dixon-Coles-adjusted bivariate Poisson goal model.

Team attack/defence strengths (maintained on fixtures.Team) give expected goals
for each side; from the resulting score matrix every market we sell is just a sum
over cells. The Dixon-Coles tau correction fixes plain Poisson's well-known
under-counting of 0-0/1-1 and over-counting of 1-0/0-1 in low-scoring games.

Nothing here calls an LLM. Probabilities must come from the numbers so the
published accuracy record means something.
"""

from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

MAX_GOALS = 10
# Low-score dependence parameter. -0.1 is the usual empirical fit for football;
# refit it per-league once there's a season of settled predictions.
RHO = -0.1


def _tau(home_goals: int, away_goals: int, lambda_home: float, lambda_away: float) -> float:
    """Dixon-Coles correction, applied only to the four low-score cells."""
    if home_goals == 0 and away_goals == 0:
        return 1 - lambda_home * lambda_away * RHO
    if home_goals == 0 and away_goals == 1:
        return 1 + lambda_home * RHO
    if home_goals == 1 and away_goals == 0:
        return 1 + lambda_away * RHO
    if home_goals == 1 and away_goals == 1:
        return 1 - RHO
    return 1.0


@dataclass
class MarketProbabilities:
    home: float
    draw: float
    away: float
    over_2_5: float
    under_2_5: float
    btts_yes: float
    btts_no: float
    correct_score: dict[str, float]
    expected_goals: tuple[float, float]

    def as_dict(self) -> dict:
        return {
            "1x2": {"home": self.home, "draw": self.draw, "away": self.away},
            "ou_2_5": {"over": self.over_2_5, "under": self.under_2_5},
            "btts": {"yes": self.btts_yes, "no": self.btts_no},
            "cs_top5": dict(sorted(self.correct_score.items(), key=lambda kv: -kv[1])[:5]),
            "xg": {"home": self.expected_goals[0], "away": self.expected_goals[1]},
        }


def score_matrix(lambda_home: float, lambda_away: float) -> np.ndarray:
    """Joint probability of every scoreline up to MAX_GOALS, DC-corrected."""
    home_probs = poisson.pmf(np.arange(MAX_GOALS + 1), lambda_home)
    away_probs = poisson.pmf(np.arange(MAX_GOALS + 1), lambda_away)
    matrix = np.outer(home_probs, away_probs)

    for h in range(2):
        for a in range(2):
            matrix[h, a] *= _tau(h, a, lambda_home, lambda_away)

    # The correction breaks normalisation; renormalise so probabilities sum to 1.
    return matrix / matrix.sum()


def expected_goals(
    home_attack: float,
    home_defence: float,
    away_attack: float,
    away_defence: float,
    league_avg_goals: float = 1.35,
    home_advantage: float = 1.15,
    *,
    league_home_avg: float | None = None,
    league_away_avg: float | None = None,
) -> tuple[float, float]:
    """
    Expected goals for each side. Strengths are multiplicative ratios around 1.0
    (1.2 attack = scores 20% more than the league average side).

    Two modes:

    * **Venue mode** (pass `league_home_avg`/`league_away_avg`): the strengths
      handed in are the venue splits, already normalised against these same
      venue averages, so home advantage is *inside the baseline* and measured
      per league. `home_advantage` is deliberately ignored here — applying it on
      top would count the home edge twice, once in the average and once in the
      multiplier, and quietly inflate every home lambda.
    * **Symmetric mode** (the default): venue-agnostic strengths against a single
      league average, with the flat `home_advantage` multiplier. Kept for callers
      that have no venue split — a brand-new league, or a team whose ratings have
      never been refreshed.
    """
    if league_home_avg is not None and league_away_avg is not None:
        lambda_home = home_attack * away_defence * league_home_avg
        lambda_away = away_attack * home_defence * league_away_avg
    else:
        lambda_home = home_attack * away_defence * league_avg_goals * home_advantage
        lambda_away = away_attack * home_defence * league_avg_goals
    # Clamp: a runaway strength ratio otherwise produces absurd 6-goal expectations.
    return (min(max(lambda_home, 0.15), 5.0), min(max(lambda_away, 0.15), 5.0))


def market_probabilities(lambda_home: float, lambda_away: float) -> MarketProbabilities:
    matrix = score_matrix(lambda_home, lambda_away)

    home = float(np.tril(matrix, -1).sum())
    draw = float(np.trace(matrix))
    away = float(np.triu(matrix, 1).sum())

    goals = np.add.outer(np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1))
    over = float(matrix[goals > 2.5].sum())

    btts_yes = float(matrix[1:, 1:].sum())

    correct_score = {
        f"{h}-{a}": float(matrix[h, a]) for h in range(6) for a in range(6)
    }

    return MarketProbabilities(
        home=home,
        draw=draw,
        away=away,
        over_2_5=over,
        under_2_5=1 - over,
        btts_yes=btts_yes,
        btts_no=1 - btts_yes,
        correct_score=correct_score,
        expected_goals=(lambda_home, lambda_away),
    )


def elo_win_probability(home_elo: float, away_elo: float, home_advantage: float = 65.0) -> float:
    """Elo prior, blended with the Poisson output to stabilise small samples."""
    diff = (home_elo + home_advantage) - away_elo
    return 1.0 / (1.0 + 10 ** (-diff / 400))


def fair_odds(probability: float) -> float:
    return round(1 / probability, 3) if probability > 0 else 0.0


def edge(market_odds: float, probability: float) -> float:
    """Positive means the book pays more than the model's fair price."""
    return (market_odds * probability) - 1.0
