"""
Accounts — Telegram-first.

Users arrive through the bot, so `telegram_id` is the real identity and
username/password exist only so Django admin and the phase-3 web app work. The
web app and the Mini App both resolve to the same User row: the Mini App passes
Telegram `initData` (verified against the bot token), the public web app uses
email + JWT.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

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


def _link_code() -> str:
    # URL-safe and unguessable: this string is the entire credential between the
    # browser that made it and the Telegram account that claims it.
    return secrets.token_urlsafe(24)


class LinkCode(TimeStampedModel):
    """
    A one-time code that connects a browser to a Telegram account.

    The browser asks for a code, opens the bot with it, and polls. Tapping Start
    in Telegram claims it; the next poll exchanges it for tokens. The user never
    types a phone number and never signs into Telegram on the web — they connect
    to the bot, which is what they think they are doing.

    Three properties carry the security, because the code alone grants a session:

    * **Unguessable** — 24 bytes of urandom. Enumeration is the obvious attack.
    * **Short-lived** — an abandoned code must not sit claimable for a week.
    * **Single use** — consumed the moment tokens are issued, so a code captured
      from a shared screen or a chat log cannot be replayed.
    """

    TTL = timedelta(minutes=10)

    code = models.CharField(max_length=64, unique=True, default=_link_code, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="link_codes",
    )
    expires_at = models.DateTimeField(db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.code[:8]}… {'claimed' if self.user_id else 'pending'}"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + self.TTL
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_claimable(self) -> bool:
        return self.user_id is None and self.consumed_at is None and not self.is_expired

    @property
    def is_redeemable(self) -> bool:
        return self.user_id is not None and self.consumed_at is None and not self.is_expired
