"""User provisioning shared by the bot and the Mini App."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import CreditEntry, Wallet

from .models import LinkCode, User


@transaction.atomic
def get_or_create_from_telegram(
    telegram_id: int, username: str = "", language_code: str = "en",
    referral_code: str = "", source: str = User.Source.TELEGRAM,
) -> tuple[User, bool]:
    """
    Idempotent by telegram_id, so the bot and the Mini App resolve the same row —
    a user who starts in chat and opens the Mini App is one account, one wallet.
    """
    user, created = User.objects.get_or_create(
        telegram_id=telegram_id,
        defaults={
            "username": f"tg_{telegram_id}",
            "telegram_username": username or "",
            "language_code": language_code or "en",
            "source": source,
        },
    )
    if not created:
        return user, False

    wallet = Wallet.objects.create(user=user)
    wallet.grant(settings.FREE_CREDITS_ON_SIGNUP, CreditEntry.Reason.SIGNUP)

    referrer = (
        User.objects.filter(referral_code=referral_code).first() if referral_code else None
    )
    if referrer and referrer != user:
        user.referred_by = referrer
        user.save(update_fields=["referred_by"])
        for party in (user, referrer):
            w, _ = Wallet.objects.get_or_create(user=party)
            w.grant(
                settings.REFERRAL_BONUS_CREDITS,
                CreditEntry.Reason.REFERRAL,
                ref=f"user:{user.pk}",
            )
    return user, True


# --- Connecting a browser to a Telegram account ---------------------------
#
# The browser asks for a code, opens the bot with it, and polls. Tapping Start
# claims it; the next poll exchanges it for a session. Nobody types a phone
# number and nobody signs into Telegram on the web — they connect to the bot,
# which is what they believe they are doing.


def create_link_code() -> LinkCode:
    return LinkCode.objects.create()


@transaction.atomic
def claim_link_code(code: str, user: User) -> bool:
    """
    Bot side: attach a Telegram account to a pending code.

    `select_for_update` because two taps on Start arrive as two updates, and
    without the lock both would pass the claimable check.
    """
    row = LinkCode.objects.select_for_update().filter(code=code).first()
    if row is None or not row.is_claimable:
        return False
    row.user = user
    row.claimed_at = timezone.now()
    row.save(update_fields=["user", "claimed_at", "updated_at"])
    return True


@transaction.atomic
def redeem_link_code(code: str) -> User | None:
    """
    Web side: exchange a claimed code for the account, once.

    Consumed on the way out. A code read off a shared screen, a chat log or a
    server log must not still be worth a session afterwards.
    """
    # `of=("self",)` locks the LinkCode row only. Without it, select_related on
    # the nullable `user` FK builds a LEFT OUTER JOIN and Postgres refuses:
    # "FOR UPDATE cannot be applied to the nullable side of an outer join".
    row = (
        LinkCode.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(code=code)
        .first()
    )
    if row is None or not row.is_redeemable:
        return None
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at", "updated_at"])
    return row.user
