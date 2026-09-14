"""
Build a slip to hit a target odds range.

"Give me 3-5 odds" is a request for a *payout*, not a leg count: the customer
wants a slip that pays roughly 3x to 5x, and does not care whether that takes
two legs or five. So the leg count falls out of the arithmetic rather than being
chosen up front.

Among the combinations that land in range, the best one is the most likely to
actually win — the product of the legs' probabilities. That is the honest
objective, and it is not the same as picking the highest-confidence legs: three
legs at 80% (51% combined) beat two at 70% (49%) even though the individual
numbers look worse.
"""

import logging
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations

from django.db.models import Q

from .models import Prediction
from .publishing import SAFE_MARKETS

logger = logging.getLogger(__name__)

# The search is over legs, but the binding constraint is fixtures: a slip may use
# each match once. Capping fixtures rather than legs keeps the combination count
# predictable (10 fixtures x 3 markets, choosing 5, is ~140k) while leaving the
# optimiser a real choice of price on every match it picks.
MAX_FIXTURES = 10
MAX_MARKETS_PER_FIXTURE = 3
# Beyond five legs the combined probability collapses — a 6-fold of 75% legs wins
# about one time in five. If a target needs that many, it is not reachable at a
# standard worth publishing.
MAX_LEGS = 5
MIN_LEGS = 2


@dataclass
class OddsResult:
    """
    Why there is or isn't a slip.

    A single "no combination found" covers three completely different
    situations — no prices in the database at all, too few priced matches, and a
    slate that genuinely cannot reach the payout — and only the last is
    something the user can act on. Telling them to "try a lower target" when we
    simply have no odds sends them round a loop that cannot succeed.
    """

    slip: "OddsSlip | None"
    reason: str                     # ok | no_prices | too_few_matches | unreachable
    priced_fixtures: int = 0
    best_available: Decimal | None = None   # closest payout we could build

    @property
    def found(self) -> bool:
        return self.slip is not None


@dataclass
class OddsSlip:
    legs: list
    combined_odds: Decimal
    combined_probability: float

    @property
    def confidence(self) -> int:
        return int(round(self.combined_probability * 100))


def candidate_legs(
    start: date,
    end: date | None = None,
    min_confidence: int = 60,
    max_fixtures: int = MAX_FIXTURES,
) -> list[Prediction]:
    """
    Priced, published, safe-market picks across a date range.

    Deliberately keeps SEVERAL markets per fixture. Taking only the most
    confident pick per match sounds right and is wrong for this job: the safety
    markets are always both the most confident and the shortest-priced, so a
    one-leg-per-fixture pool is entirely double chance at ~1.12 and no target
    above about 2.0 is ever reachable. The optimiser needs the option to take a
    1X2 leg at 1.40 on the same match instead.

    One-leg-per-fixture is still enforced — but at selection time, in
    `build_for_target`, where it belongs.
    """
    end = end or start
    rows = (
        Prediction.objects.filter(
            Q(fixture__kickoff__date__gte=start) & Q(fixture__kickoff__date__lte=end),
            published_at__isnull=False,
            confidence__gte=min_confidence,
            market_odds__isnull=False,
            market__in=SAFE_MARKETS,
        )
        .select_related("fixture__home", "fixture__away", "fixture__league")
        .order_by("-confidence")
    )

    by_fixture: dict[int, list] = {}
    for row in rows:
        if row.market_odds is None or row.market_odds <= 1:
            continue
        legs = by_fixture.setdefault(row.fixture_id, [])
        if len(legs) < MAX_MARKETS_PER_FIXTURE:
            legs.append(row)

    # Rank fixtures by their strongest pick, keep the best few, flatten.
    ranked = sorted(
        by_fixture.values(), key=lambda legs: -max(leg.confidence for leg in legs)
    )[:max_fixtures]
    return [leg for legs in ranked for leg in legs]


def build_for_target(
    start: date,
    end: date | None = None,
    min_odds: float = 3.0,
    max_odds: float = 5.0,
    min_confidence: int = 60,
) -> OddsResult:
    """
    The combination landing inside [min_odds, max_odds] with the best chance of
    winning, or a reason why there isn't one.

    Never pads to reach a target: adding a long shot to hit 5.00 is how a public
    record gets ruined, and the whole product rests on that record.
    """
    if min_odds <= 1 or max_odds < min_odds:
        raise ValueError(f"nonsensical odds range: {min_odds}-{max_odds}")

    pool = candidate_legs(start, end, min_confidence)
    fixtures = len({leg.fixture_id for leg in pool})

    if not pool:
        # Either nothing is published for this window, or nothing published
        # carries a price. Both are ours to fix, not the user's.
        logger.info("odds builder: no priced legs for %s..%s", start, end or start)
        return OddsResult(None, "no_prices")

    if fixtures < MIN_LEGS:
        return OddsResult(None, "too_few_matches", priced_fixtures=fixtures)

    log_min, log_max = math.log(min_odds), math.log(max_odds)
    priced = [(leg, math.log(float(leg.market_odds)), math.log(leg.probability)) for leg in pool]

    best: tuple[float, list] | None = None
    # Tracked so an unreachable target can say what IS reachable, which is the
    # one piece of advice the user can actually act on.
    closest: float | None = None

    for size in range(MIN_LEGS, min(MAX_LEGS, len(priced)) + 1):
        for combo in combinations(priced, size):
            # One leg per fixture. Two picks on the same match are correlated, so
            # multiplying their odds overstates the payout for the risk taken —
            # the slip is a worse bet than its headline number claims.
            if len({c[0].fixture_id for c in combo}) != size:
                continue

            log_odds = sum(c[1] for c in combo)
            if closest is None or abs(log_odds - log_min) < abs(closest - log_min):
                closest = log_odds

            if log_odds < log_min or log_odds > log_max:
                continue
            log_prob = sum(c[2] for c in combo)
            if best is None or log_prob > best[0]:
                best = (log_prob, [c[0] for c in combo])

    if best is None:
        return OddsResult(
            None, "unreachable",
            priced_fixtures=fixtures,
            best_available=(
                Decimal(str(round(math.exp(closest), 2))) if closest is not None else None
            ),
        )

    legs = sorted(best[1], key=lambda p: p.fixture.kickoff)
    combined = Decimal("1.0")
    for leg in legs:
        combined *= leg.market_odds

    return OddsResult(
        OddsSlip(
            legs=legs,
            combined_odds=combined.quantize(Decimal("0.01")),
            combined_probability=math.exp(best[0]),
        ),
        "ok",
        priced_fixtures=fixtures,
    )
