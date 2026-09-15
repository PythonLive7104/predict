"""
Entitlement rules — the single source of truth for "what may this user see".

Every surface calls these: the Telegram bot (via sync_to_async wrappers in
apps/bot/services.py), the REST API, and anything added later. Nothing here
knows about aiogram, DRF, or HTTP, and no surface is allowed to reimplement a
rule locally — that is how a bot and a web app start disagreeing about who paid
for what.
"""

from dataclasses import dataclass
from datetime import date, timedelta

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


def is_admin(user) -> bool:
    """
    Superusers and staff see everything, with no expiry.

    The owner should not have to buy their own product to check that it works,
    and anyone who can reach the Django admin already reads every prediction
    there — so gating the bot against them protects nothing and only makes the
    product harder to run.

    Both flags are checked because they are independent: a superuser without
    `is_staff` is unusual but legal, and would otherwise be locked out of the
    thing they own.
    """
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_staff", False))


def has_vip(user) -> bool:
    """
    One definition of "may see VIP content", shared by the entitlement and the
    broadcast audience.

    Kept separate from `entitlement_for` because the audience filter walks every
    reachable user, and `entitlement_for` creates a Wallet as a side effect —
    fine for one user, a write per row in a loop.
    """
    if is_admin(user):
        return True
    active = current_subscription(user)
    return bool(active and active.plan.includes_vip_slips)


def entitlement_for(user) -> Entitlement:
    wallet, _ = Wallet.objects.get_or_create(user=user)

    if is_admin(user):
        # No expiry: an admin's access is a property of the account, not a
        # subscription that lapses. `expires_at=None` renders as "no renewal
        # date" everywhere a plan is shown, which is the truth.
        return Entitlement(
            plan_name="Admin",
            expires_at=None,
            unlimited=True,
            vip=True,
            credits=wallet.balance,
        )

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


def published_picks(
    on: date | None = None,
    tier: str | None = None,
    until: date | None = None,
    league_id: int | None = None,
):
    """
    Published picks for one day, or a span when `until` is given.

    `published_at__isnull=False` is not optional alongside a tier filter: tier
    defaults to FREE on unpublished rows, so filtering on tier alone counts picks
    that never went out.
    """
    on = on or timezone.now().date()
    qs = (
        Prediction.objects.filter(
            published_at__isnull=False,
            fixture__kickoff__date__gte=on,
            fixture__kickoff__date__lte=until or on,
        )
        .select_related("fixture__home", "fixture__away", "fixture__league")
        .order_by("fixture__kickoff", "-confidence")
    )
    if league_id:
        qs = qs.filter(fixture__league__api_id=league_id)
    return qs.filter(tier=tier) if tier else qs


def leagues_with_picks(start: date, end: date | None = None) -> list[dict]:
    """
    Leagues that actually have published picks in this window, with counts.

    Only leagues with something to show are listed. Offering fifty and having
    forty-six of them answer "nothing published" is worse than offering four
    that do.
    """
    rows = (
        Prediction.objects.filter(
            published_at__isnull=False,
            fixture__kickoff__date__gte=start,
            fixture__kickoff__date__lte=end or start,
        )
        .values("fixture__league__api_id", "fixture__league__name",
                "fixture__league__country")
        .annotate(picks=Count("id"))
        .order_by("-picks", "fixture__league__name")
    )
    return [
        {
            "api_id": r["fixture__league__api_id"],
            "name": r["fixture__league__name"],
            "country": r["fixture__league__country"],
            "picks": r["picks"],
        }
        for r in rows
    ]


def span_dates(span: str, today: date | None = None) -> tuple[date, date, str]:
    """
    Resolve a named span to (start, end, label).

    "Weekend" means Friday through Sunday — and from Monday to Thursday that is
    the *coming* weekend, while on Saturday it is the one in progress. Users ask
    for "weekend picks" meaning whichever weekend they can still bet on.
    """
    today = today or timezone.now().date()
    if span == "tomorrow":
        day = today + timedelta(days=1)
        return day, day, "Tomorrow"
    if span == "weekend":
        # Monday is 0, Friday 4, Sunday 6.
        if today.weekday() >= 4:
            friday = today - timedelta(days=today.weekday() - 4)
        else:
            friday = today + timedelta(days=4 - today.weekday())
        return friday, friday + timedelta(days=2), "This weekend"
    return today, today, "Today"


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
