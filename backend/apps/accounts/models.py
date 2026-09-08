"""
Accounts — Telegram-first.

Users arrive through the bot, so `telegram_id` is the real identity and
username/password exist only so Django admin and the phase-3 web app work. The
web app and the Mini App both resolve to the same User row: the Mini App passes
Telegram `initData` (verified against the bot token), the public web app uses
email + JWT.
"""

import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.common.models import TimeStampedModel


def _referral_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()


class User(AbstractUser, TimeStampedModel):
    class Source(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        WEB = "web", "Web"

    telegram_id = models.BigIntegerField(unique=True, null=True, blank=True, db_index=True)
    telegram_username = models.CharField(max_length=64, blank=True)
    language_code = models.CharField(max_length=8, default="en")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.TELEGRAM)

    # Referrals drive most of the growth in this niche — both sides get credits.
    referral_code = models.CharField(max_length=12, unique=True, default=_referral_code)
    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals"
    )

    # Set when Telegram tells us the user blocked the bot. Never guessed.
    is_blocked = models.BooleanField(default=False)
    # Opt-out. Push is the retention engine here, but sending to someone who
    # muted us is how a bot gets mass-reported and banned outright.
    notifications_enabled = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.telegram_username or self.username or f"tg:{self.telegram_id}"
