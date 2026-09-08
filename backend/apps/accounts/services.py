"""User provisioning shared by the bot and the Mini App."""

from django.conf import settings
from django.db import transaction

from apps.billing.models import CreditEntry, Wallet

from .models import User


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
