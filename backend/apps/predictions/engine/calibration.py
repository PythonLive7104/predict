"""
Correct the engine's systematic over-confidence.

Measured over 22,660 graded picks across three real seasons, every confidence
band came in 5-13 points below its claim, and the relationship was almost
perfectly linear:

    actual = 4.11 + 0.808 x claimed      (max residual 3.1 points)

Why it happens is structural, not a bug. The Poisson model treats its estimated
scoring rates as exact, when they are estimates carrying real error. Ignoring
that error makes the score distribution too narrow, which pushes every
probability toward its nearest extreme. Shrinking the strengths helps the
estimates; it does nothing about the model's certainty in them.

Why it matters is commercial. The product is sold on the confidence figure
meaning what it says. A pick claiming 80% that wins 69% is a false claim made in
public, and the record that makes the product trustworthy is the same record
that will eventually expose it.

**Refit this.** These constants come from one backtest over two leagues. Re-run
`manage.py backtest` after a season of live picks and re-fit — the calibration
table prints exactly the numbers the fit needs.
"""

from django.conf import settings

# Probabilities are clamped away from certainty afterwards. Nothing in football
# is 98% and a model that says so is describing its own arithmetic, not a match.
FLOOR, CEILING = 0.02, 0.95


def calibrate(probability: float) -> float:
    """
    Map a raw model probability to one that matches observed outcomes.

    Applied to the selected candidate rather than to the whole score matrix: the
    matrix has to stay internally coherent (its cells sum to 1), while what needs
    correcting is the number shown to a user and used to decide publication.
    """
    slope = settings.CALIBRATION_SLOPE
    intercept = settings.CALIBRATION_INTERCEPT
    if slope == 1.0 and intercept == 0.0:
        return probability

    adjusted = intercept + slope * probability
    return min(max(adjusted, FLOOR), CEILING)
