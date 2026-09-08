"""
What goes out, to whom, and as what.

Generation (engine/pipeline.py) prices every fixture; publishing decides which of
those picks the world sees. The split matters commercially: free picks are the
shop window and must be genuinely good, VIP is depth — more fixtures, more
markets, plus the curated slips.
"""

import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Market, Outcome, Prediction, Slip, Tier

logger = logging.getLogger(__name__)

# Free picks per day. Small enough to leave a reason to pay, good enough that the
# public record built from them is worth showing.
FREE_PICKS_PER_DAY = 3
# A slip leg must clear this even when the single-pick gate is lower — a 5-fold
# of 55% picks wins about one time in twenty.
MIN_SLIP_LEG_CONFIDENCE = 65
TWO_ODDS_TARGET = Decimal("2.0")
# Markets allowed in the safety-first slips, in preference order.
SAFE_MARKETS = [Market.DOUBLE_CHANCE, Market.MATCH_RESULT, Market.OVER_UNDER_25]


def _publishable_for(on: date):
    return (
        Prediction.objects.filter(
            fixture__kickoff__date=on,
            published_at__isnull=True,
            confidence__gte=settings.MIN_PUBLISH_CONFIDENCE,
        )
        .select_related("fixture__home", "fixture__away", "fixture__league")
        .order_by("-confidence")
    )


@transaction.atomic
def publish_daily(on: date | None = None, free_count: int = FREE_PICKS_PER_DAY) -> dict:
    """
    Publish the day's slate. The best picks go free — deliberately, not the
    leftovers: the free tier is what the public record is judged on.

    One free pick per fixture at most, so the shop window shows breadth rather
    than four angles on the same match.
    """
    on = on or timezone.now().date()
    candidates = list(_publishable_for(on))

    free_published, seen_fixtures, seen_markets = 0, set(), set()
    for prediction in candidates:
        # Two diversity rules, both about what the free slate is *for*.
        #
        # One pick per fixture: show breadth, not four angles on one match.
        #
        # One pick per market: ranking on confidence alone hands every free slot
        # to Double Chance, which is ~85-92% by construction because it combines
        # two outcomes. Three DC picks at 1.12 is a shop window of nothing a
        # bettor would use — and it makes the public record look inflated while
        # saying nothing about whether the model can call a winner.
        wants_free = (
            free_published < free_count
            and prediction.fixture_id not in seen_fixtures
            and prediction.market not in seen_markets
        )
        tier = Tier.FREE if wants_free else Tier.VIP
        if prediction.publish(tier=tier) and wants_free:
            free_published += 1
            seen_fixtures.add(prediction.fixture_id)
            seen_markets.add(prediction.market)

    result = {
        "date": on.isoformat(),
        "free": free_published,
        "vip": len(candidates) - free_published,
        "total": len(candidates),
    }
    logger.info("published %s", result)
    return result


def _slip_legs(on: date, limit: int, min_confidence: int = MIN_SLIP_LEG_CONFIDENCE):
    """
    One leg per fixture, priced, safe markets only, most confident first.

    Correlated legs are the classic accumulator mistake — two picks on the same
    match aren't independent, so the combined odds lie about the real risk.
    """
    legs, seen = [], set()
    qs = (
        Prediction.objects.filter(
            fixture__kickoff__date=on,
            published_at__isnull=False,
            confidence__gte=min_confidence,
            market_odds__isnull=False,
            market__in=SAFE_MARKETS,
        )
        .select_related("fixture__home", "fixture__away")
        .order_by("-confidence")
    )
    for prediction in qs:
        if prediction.fixture_id in seen:
            continue
        legs.append(prediction)
        seen.add(prediction.fixture_id)
        if len(legs) >= limit:
            break
    return legs


def _combined(legs) -> Decimal:
    total = Decimal("1.0")
    for leg in legs:
        total *= leg.market_odds
    return total.quantize(Decimal("0.001"))


@transaction.atomic
def build_slips(on: date | None = None) -> list[Slip]:
    """
    Build the day's curated slips. These are what actually convert — users buy a
    slip, not a probability table.

    Returns only the slips that could be built; a thin slate legitimately produces
    none, and shipping a padded slip to hit a quota is how a record gets ruined.
    """
    on = on or timezone.now().date()
    pool = _slip_legs(on, limit=8)
    if not pool:
        logger.info("no slip-eligible picks for %s", on)
        return []

    built = []

    # Banker — the single most confident pick of the day.
    banker = pool[0]
    built.append(
        _upsert_slip(
            kind=Slip.Kind.BANKER,
            title=f"Banker of the Day — {on:%d %b}",
            on=on,
            legs=[banker],
            tier=Tier.VIP,
        )
    )

    # 2 Odds Daily — fewest legs that clear 2.0, safest first. Adding the safest
    # legs first means the slip reaches the target with the least risk taken,
    # rather than the fewest legs.
    acc, running = [], Decimal("1.0")
    for leg in pool:
        acc.append(leg)
        running *= leg.market_odds
        if running >= TWO_ODDS_TARGET:
            break
    if running >= TWO_ODDS_TARGET:
        built.append(
            _upsert_slip(
                kind=Slip.Kind.TWO_ODDS,
                title=f"2 Odds Daily — {on:%d %b}",
                on=on,
                legs=acc,
                tier=Tier.VIP,
            )
        )

    # Accumulator — up to four legs for the higher-risk appetite.
    if len(pool) >= 3:
        built.append(
            _upsert_slip(
                kind=Slip.Kind.ACCUMULATOR,
                title=f"{min(len(pool), 4)}-Fold Accumulator — {on:%d %b}",
                on=on,
                legs=pool[:4],
                tier=Tier.VIP,
            )
        )

    return built


def _upsert_slip(kind: str, title: str, on: date, legs: list, tier: str) -> Slip:
    slip, _ = Slip.objects.update_or_create(
        kind=kind,
        for_date=on,
        defaults={
            "title": title,
            "tier": tier,
            "total_odds": _combined(legs),
            "published_at": timezone.now(),
        },
    )
    slip.predictions.set(legs)
    return slip


def settle_slips(on: date | None = None) -> int:
    """
    A slip wins only if every leg wins. Pending until all legs are decided —
    a lost leg settles it immediately, since nothing later can save it.
    """
    on = on or timezone.now().date()
    settled = 0

    for slip in Slip.objects.filter(for_date=on, outcome=Outcome.PENDING).prefetch_related(
        "predictions"
    ):
        outcomes = [p.outcome for p in slip.predictions.all()]
        if not outcomes:
            continue
        if Outcome.LOST in outcomes:
            slip.outcome = Outcome.LOST
        elif all(o == Outcome.WON for o in outcomes):
            slip.outcome = Outcome.WON
        else:
            continue  # still legs in play
        slip.save(update_fields=["outcome", "updated_at"])
        settled += 1

    return settled
