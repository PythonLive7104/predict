"""Generation, settlement and the accuracy rollup that backs the public record."""

import logging
from datetime import date, timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from apps.fixtures.models import Fixture

from .engine import llm, ratings
from .engine.pipeline import ENGINE_VERSION, generate_for_fixture
from .models import AccuracySnapshot, Market, Outcome, Prediction, RationaleBatch
from .publishing import build_slips, publish_daily, settle_slips

logger = logging.getLogger(__name__)


@shared_task
def generate_daily_predictions(days_ahead: int = 1) -> int:
    """Run a few hours before the first kickoff, once lineups and odds have settled."""
    window_end = timezone.now() + timedelta(days=days_ahead)
    fixtures = Fixture.objects.filter(
        status=Fixture.Status.SCHEDULED,
        kickoff__gte=timezone.now(),
        kickoff__lte=window_end,
        league__is_active=True,
    ).select_related("home", "away", "league")

    # In batch mode the numbers are produced here and the prose is bought later
    # at half price. Generation therefore makes no API calls at all and finishes
    # in seconds, which is also why one slow provider can no longer stall a slate.
    inline_prose = not settings.LLM_BATCH_ENABLED

    total = 0
    for fixture in fixtures:
        try:
            total += len(generate_for_fixture(fixture, with_rationale=inline_prose))
        except Exception:
            # One bad fixture must not take down the whole slate.
            logger.exception("generation failed for fixture %s", fixture.pk)

    if settings.LLM_BATCH_ENABLED:
        try:
            # Chained rather than scheduled, so the batch can never be submitted
            # for a slate that has not been generated yet.
            submit_rationale_batch.delay()
        except Exception:
            # A broker hiccup must not sink a slate that is already priced and
            # stored. The picks are publishable; the next collect run will find
            # them still waiting for prose.
            logger.exception("could not queue the rationale batch")
    return total


def predictions_awaiting_prose():
    """
    Publishable picks with no rationale and no batch already carrying them.

    Confidence is the same gate publishing uses: paying to write prose for a pick
    that will never ship is pure waste.
    """
    return Prediction.objects.filter(
        rationale="",
        rationale_batch__isnull=True,
        engine_version=ENGINE_VERSION,
        confidence__gte=settings.MIN_PUBLISH_CONFIDENCE,
        fixture__status=Fixture.Status.SCHEDULED,
        fixture__kickoff__gte=timezone.now(),
        # Team names are needed for the selection label on every row, so pull
        # them in the same query rather than one per pick.
    ).select_related("fixture__home", "fixture__away")


@shared_task
def submit_rationale_batch() -> int:
    """
    Hand every prose-less publishable pick to the batch endpoint.

    Non-fatal by contract: if this fails the picks still publish, just without a
    read, exactly as when the synchronous call fails.
    """
    pending = list(predictions_awaiting_prose())
    if not pending:
        logger.info("rationale batch: nothing to submit")
        return 0

    items = []
    for prediction in pending:
        pack = prediction.stat_snapshot or {}
        items.append((
            f"pred-{prediction.pk}",
            llm.request_body(
                pack,
                pack.get("model_output", {}),
                prediction.selection,
                prediction.market,
                prediction.selection_label,
                prediction.get_market_display(),
            ),
        ))

    try:
        payload = llm.submit_batch(items, description=f"rationales {timezone.now():%Y-%m-%d}")
    except Exception:
        logger.exception("rationale batch submission failed; picks ship without prose")
        return 0

    batch = RationaleBatch.objects.create(
        provider_batch_id=payload["id"],
        input_file_id=payload.get("input_file_id") or "",
        status=payload.get("status") or RationaleBatch.Status.VALIDATING,
        requested=len(items),
        for_date=timezone.now().date(),
        raw=payload,
    )
    # Claim the picks only after the provider has accepted the batch — claiming
    # first would strand them if submission threw.
    Prediction.objects.filter(pk__in=[p.pk for p in pending]).update(rationale_batch=batch)
    return len(items)


@shared_task
def collect_rationale_batches() -> int:
    """Poll open batches and backfill prose onto the picks they were built for."""
    backfilled = 0

    for batch in RationaleBatch.objects.filter(status__in=RationaleBatch.OPEN_STATUSES):
        try:
            payload = llm.poll_batch(batch.provider_batch_id)
        except Exception:
            logger.exception("polling batch %s failed", batch.provider_batch_id)
            continue

        batch.status = payload.get("status") or batch.status
        batch.output_file_id = payload.get("output_file_id") or ""
        batch.raw = payload

        if batch.status in RationaleBatch.DEAD_STATUSES:
            released = batch.release_predictions()
            batch.save()
            logger.warning(
                "batch %s ended %s; released %s picks for retry",
                batch.provider_batch_id, batch.status, released,
            )
            continue

        if batch.status != RationaleBatch.Status.COMPLETED:
            batch.save()
            continue

        backfilled += _apply_batch_results(batch)

    return backfilled


def _apply_batch_results(batch: RationaleBatch) -> int:
    if not batch.output_file_id:
        logger.warning("batch %s completed with no output file", batch.provider_batch_id)
        batch.save()
        return 0

    try:
        results = llm.fetch_batch_results(batch.output_file_id)
    except Exception:
        logger.exception("fetching results for %s failed", batch.provider_batch_id)
        return 0

    applied = 0
    for prediction in batch.predictions.all():
        read = results.get(f"pred-{prediction.pk}")
        if not read:
            continue
        prediction.rationale = read["rationale"]
        # The stat pack stays frozen; the read is layered on top, matching what
        # the synchronous path writes so a snapshot reads the same either way.
        prediction.stat_snapshot = (prediction.stat_snapshot or {}) | {"llm": read}
        prediction.save(update_fields=["rationale", "stat_snapshot", "updated_at"])
        applied += 1

    batch.collected = applied
    batch.collected_at = timezone.now()
    batch.save()
    logger.info(
        "batch %s: backfilled %s of %s rationales",
        batch.provider_batch_id, applied, batch.requested,
    )
    return applied


@shared_task
def settle_predictions() -> int:
    """Grade every pending pick whose fixture has finished."""
    pending = Prediction.objects.filter(
        outcome=Outcome.PENDING, fixture__status=Fixture.Status.FINISHED
    ).select_related("fixture")

    settled = 0
    for prediction in pending:
        outcome = grade(prediction)
        if outcome is None:
            continue
        prediction.outcome = outcome
        prediction.settled_at = timezone.now()
        prediction.save(update_fields=["outcome", "settled_at", "updated_at"])
        settled += 1

    if settled:
        # Order matters: Elo before the rollup, so the ratings that produced
        # tomorrow's picks already reflect tonight's results.
        for fixture in Fixture.objects.filter(
            status=Fixture.Status.FINISHED, home_goals__isnull=False, elo_applied=False
        ).select_related("home", "away").order_by("kickoff"):
            ratings.apply_elo(fixture)
        settle_slips()
        rebuild_accuracy.delay()
    return settled


def grade(prediction: Prediction) -> str | None:
    """Returns WON/LOST/VOID, or None when the fixture isn't gradeable yet."""
    fx = prediction.fixture
    if not fx.is_settled:
        return None
    home, away = fx.home_goals, fx.away_goals
    total = home + away
    sel = prediction.selection

    match prediction.market:
        case Market.MATCH_RESULT:
            actual = "home" if home > away else "away" if away > home else "draw"
            return Outcome.WON if sel == actual else Outcome.LOST
        case Market.OVER_UNDER_25:
            over = total > 2.5
            return Outcome.WON if (sel == "over") == over else Outcome.LOST
        case Market.BTTS:
            btts = home > 0 and away > 0
            return Outcome.WON if (sel == "yes") == btts else Outcome.LOST
        case Market.DOUBLE_CHANCE:
            actual = "home" if home > away else "away" if away > home else "draw"
            return Outcome.WON if actual in sel.split("_") else Outcome.LOST
        case Market.CORRECT_SCORE:
            return Outcome.WON if sel == f"{home}-{away}" else Outcome.LOST
    return None


@shared_task
def rebuild_accuracy(days: int = 90) -> int:
    """
    Recompute the daily rollups. Cheap enough to redo wholesale, and doing it that
    way means a corrected result can never leave a stale win-rate on the site.
    """
    since = date.today() - timedelta(days=days)
    rows = (
        Prediction.objects.filter(
            published_at__isnull=False, fixture__kickoff__date__gte=since
        )
        .values("fixture__kickoff__date", "market", "tier")
        .annotate(
            total=Count("id"),
            won=Count("id", filter=Q(outcome=Outcome.WON)),
            lost=Count("id", filter=Q(outcome=Outcome.LOST)),
            void=Count("id", filter=Q(outcome=Outcome.VOID)),
        )
    )

    written = 0
    for row in rows:
        AccuracySnapshot.objects.update_or_create(
            for_date=row["fixture__kickoff__date"],
            market=row["market"],
            tier=row["tier"],
            defaults={
                "total": row["total"],
                "won": row["won"],
                "lost": row["lost"],
                "void": row["void"],
                "roi": _roi(row["fixture__kickoff__date"], row["market"], row["tier"]),
            },
        )
        written += 1
    return written


def _roi(on: date, market: str, tier: str) -> float | None:
    """Return on 1-unit level stakes at the price we published."""
    qs = Prediction.objects.filter(
        fixture__kickoff__date=on,
        market=market,
        tier=tier,
        published_at__isnull=False,
        market_odds__isnull=False,
    ).exclude(outcome__in=[Outcome.PENDING, Outcome.VOID])

    staked = qs.count()
    if not staked:
        return None
    returned = sum(float(p.market_odds) for p in qs.filter(outcome=Outcome.WON))
    return (returned - staked) / staked


@shared_task
def refresh_ratings() -> dict:
    """
    Nightly rating maintenance. Runs after settlement so the strengths feeding
    tomorrow's slate include tonight's results.
    """
    return ratings.refresh_all()


@shared_task
def publish_and_build_slips(notify: bool = True) -> dict:
    """
    The publish step, run after generation. Kept separate from generation so a
    slate can be regenerated (engine bump, late team news) without republishing
    or double-notifying anyone.
    """
    published = publish_daily()
    slips = build_slips()

    queued = []
    if notify:
        queued = announce(published_free=published["free"], slips=slips)

    return published | {"slips": [s.title for s in slips], "broadcasts": queued}


def announce(published_free: int, slips: list) -> list[str]:
    """
    Queue the day's pushes. Broadcast rows are created here and handed to the bot
    worker; nothing is sent inline, so a slow Telegram never stalls publishing.
    """
    from apps.bot.notifications import build_free_picks_broadcast, build_slip_broadcast
    from apps.bot.tasks import send_broadcast
    from apps.predictions.access import published_picks
    from apps.predictions.models import Tier

    queued = []

    if published_free:
        free = list(published_picks(tier=Tier.FREE).order_by("-confidence"))
        broadcast = build_free_picks_broadcast(free)
        if broadcast:
            send_broadcast.delay(broadcast.pk)
            queued.append(broadcast.title)

    # One push per slip would be spam; the banker is the one people want.
    banker = next((s for s in slips if s.kind == "banker"), None)
    if banker:
        broadcast = build_slip_broadcast(banker)
        send_broadcast.delay(broadcast.pk)
        queued.append(broadcast.title)

    return queued
