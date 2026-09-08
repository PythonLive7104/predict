"""
Outbound push — the retention engine.

Two rules govern everything here, and both exist because violating either gets a
bot banned rather than merely annoying people:

1. **Never send to someone who opted out or blocked us.** `is_blocked` is set
   only from Telegram's own 403 response, never guessed.
2. **Never send the same thing twice.** Delivery rows are unique per
   (user, broadcast), so a retried Celery task re-sends to nobody.
"""

import logging

from django.utils import timezone

from apps.accounts.models import User
from apps.predictions.access import current_subscription
from apps.predictions.models import Slip, Tier

from .models import Broadcast

logger = logging.getLogger(__name__)


def audience(target_tier: str = "") -> "models.QuerySet[User]":
    """
    Who may receive a push right now.

    An empty `target_tier` means everyone reachable; "vip" narrows to users whose
    subscription is currently active and includes slips.
    """
    reachable = User.objects.filter(
        telegram_id__isnull=False, is_blocked=False, notifications_enabled=True
    )
    if target_tier != Tier.VIP:
        return reachable

    # Filtered in Python via the shared entitlement rule rather than re-expressed
    # as a query, so "is this subscription current" has exactly one definition.
    vip_ids = [
        user.pk
        for user in reachable.prefetch_related("subscriptions__plan")
        if (sub := current_subscription(user)) and sub.plan.includes_vip_slips
    ]
    return reachable.filter(pk__in=vip_ids)


def build_free_picks_broadcast(picks) -> Broadcast | None:
    """Teasers only — the push must not give away what the unlock is for."""
    if not picks:
        return None

    lines = [f"⚽ <b>Today's free picks</b> — {len(picks)} matches analysed", ""]
    for pick in picks:
        fixture = pick.fixture
        lines.append(
            f"• {fixture.home.name} v {fixture.away.name} — "
            f"{pick.get_market_display()} · <b>{pick.confidence}%</b>"
        )
    lines += ["", "Open the bot to unlock them."]

    return Broadcast.objects.create(
        title=f"Free picks {timezone.now():%d %b}",
        body="\n".join(lines),
        status=Broadcast.Status.DRAFT,
    )


def build_slip_broadcast(slip: Slip) -> Broadcast:
    """VIP-only: subscribers have paid, so this one carries the actual legs."""
    lines = [f"🎟 <b>{slip.title}</b>", ""]
    for leg in slip.predictions.select_related("fixture__home", "fixture__away"):
        lines.append(
            f"• {leg.fixture.home.name} v {leg.fixture.away.name} — "
            f"<b>{leg.selection_label}</b> ({leg.confidence}%)"
        )
    if slip.total_odds:
        lines += ["", f"Combined odds: <b>{slip.total_odds}</b>"]

    return Broadcast.objects.create(
        title=slip.title,
        body="\n".join(lines),
        slip=slip,
        target_tier=Tier.VIP,
        status=Broadcast.Status.DRAFT,
    )
