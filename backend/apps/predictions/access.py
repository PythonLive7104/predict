"""
Entitlement rules — the single source of truth for "what may this user see".

Every surface calls these: the Telegram bot (via sync_to_async wrappers in
apps/bot/services.py), the REST API, and anything added later. Nothing here
knows about aiogram, DRF, or HTTP, and no surface is allowed to reimplement a
rule locally — that is how a bot and a web app start disagreeing about who paid
for what.
"""

from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.billing.models import CreditEntry, Subscription, Wallet

from .models import Outcome, Prediction, PredictionView, Slip, Tier


@dataclass(frozen=True)
class Entitlement:
    plan_name: str
    expires_at: object | None
    unlimited: bool
    vip: bool
    credits: int

    @property
    def is_paid(self) -> bool:
        return self.plan_name != "Free"


def current_subscription(user) -> Subscription | None:
    subscription = (
        Subscription.objects.filter(user=user, status=Subscription.Status.ACTIVE)
        .select_related("plan")
        .order_by("-expires_at")
        .first()
    )
    return subscription if subscription and subscription.is_current else None


def entitlement_for(user) -> Entitlement:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    active = current_subscription(user)
    return Entitlement(
        plan_name=active.plan.name if active else "Free",
        expires_at=active.expires_at if active else None,
        unlimited=bool(active and active.plan.unlimited),
        vip=bool(active and active.plan.includes_vip_slips),
        credits=wallet.balance,
    )


def can_view(user, prediction: Prediction) -> bool:
    """
    Unlocked if the plan covers everything, or the user already paid for this
    pick. Free-tier picks are still gated on an unlock: the credit spend is what
    makes the free allowance meaningful.
    """
    if entitlement_for(user).unlimited:
        return True
    return PredictionView.objects.filter(user=user, prediction=prediction).exists()


def published_picks(on: date | None = None, tier: str | None = None):
    on = on or timezone.now().date()
    qs = (
        Prediction.objects.filter(published_at__isnull=False, fixture__kickoff__date=on)
        .select_related("fixture__home", "fixture__away", "fixture__league")
        .order_by("fixture__kickoff", "-confidence")
    )
    return qs.filter(tier=tier) if tier else qs


def slips_for(on: date | None = None):
    return (
        Slip.objects.filter(for_date=on or timezone.now().date(), published_at__isnull=False)
        .prefetch_related(
            "predictions__fixture__home",
            "predictions__fixture__away",
            "predictions__fixture__league",
        )
        .order_by("kind")
    )


@transaction.atomic
def unlock(user, prediction_id: int, cost: int = 1) -> tuple[Prediction | None, str]:
    """
    Returns (prediction, status): 'unlocked', 'already', 'insufficient', 'missing'.

    Atomic and idempotent — a double-tap on the unlock button, or a retried API
    call, must never charge twice.
    """
    prediction = (
        Prediction.objects.filter(pk=prediction_id, published_at__isnull=False)
        .select_related("fixture__home", "fixture__away", "fixture__league")
        .first()
    )
    if prediction is None:
        return None, "missing"

    if entitlement_for(user).unlimited:
        return prediction, "already"

    if PredictionView.objects.filter(user=user, prediction=prediction).exists():
        return prediction, "already"

    wallet, _ = Wallet.objects.get_or_create(user=user)
    if wallet.spend(cost, CreditEntry.Reason.UNLOCK, ref=f"prediction:{prediction.pk}") is None:
        return prediction, "insufficient"

    PredictionView.objects.create(user=user, prediction=prediction, credits_spent=cost)
    return prediction, "unlocked"


def record_summary(days: int = 30) -> dict:
    """
    The public accuracy record. Settled picks only, no filtering by outcome —
    the whole point is that it cannot be cherry-picked.
    """
    since = timezone.now().date() - timezone.timedelta(days=days)
    qs = Prediction.objects.filter(
        published_at__isnull=False, fixture__kickoff__date__gte=since
    ).exclude(outcome__in=[Outcome.PENDING, Outcome.VOID])

    total = qs.count()
    won = qs.filter(outcome=Outcome.WON).count()
    staked = qs.filter(market_odds__isnull=False)
    n_staked = staked.count()
    returned = sum(float(p.market_odds) for p in staked.filter(outcome=Outcome.WON))

    return {
        "days": days,
        "settled": total,
        "won": won,
        "lost": total - won,
        "win_rate": round(won / total * 100, 1) if total else 0.0,
        "roi": round((returned - n_staked) / n_staked * 100, 1) if n_staked else None,
    }


def record_by_market(days: int = 30) -> list[dict]:
    """
    Per-market breakdown — where the model is actually good, shown honestly.

    Grouped with a single aggregate rather than values_list(...).distinct().
    Prediction has Meta.ordering, and Django adds the ORDER BY column to the
    SELECT, so DISTINCT there dedupes on (market, kickoff) and every market comes
    back once per fixture.
    """
    since = timezone.now().date() - timezone.timedelta(days=days)
    rows = (
        Prediction.objects.filter(
            published_at__isnull=False, fixture__kickoff__date__gte=since
        )
        .exclude(outcome__in=[Outcome.PENDING, Outcome.VOID])
        .order_by()  # drop Meta.ordering so the GROUP BY is on market alone
        .values("market")
        .annotate(total=Count("id"), won=Count("id", filter=Q(outcome=Outcome.WON)))
        .order_by("-total")
    )

    return [
        {
            "market": row["market"],
            "total": row["total"],
            "won": row["won"],
            "win_rate": round(row["won"] / row["total"] * 100, 1) if row["total"] else 0.0,
        }
        for row in rows
    ]
