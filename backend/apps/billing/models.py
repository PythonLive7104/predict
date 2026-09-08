"""
Billing — free credits + paid tiers, settled in crypto.

Credit accounting is a ledger, not a mutable counter: every grant and spend is a
row, and Wallet.balance is a cache the ledger can always rebuild. Disputes in
this niche are constant ("the bot ate my credits") and a ledger settles them.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.common.models import TimeStampedModel


class Plan(TimeStampedModel):
    """Mirrors the tier card the bot renders under /purchase."""

    code = models.SlugField(unique=True)
    name = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    price_usd = models.DecimalField(max_digits=8, decimal_places=2)
    duration_days = models.IntegerField(help_text="30, 180, 365 …")

    # A plan grants either a credit allowance, unlimited VIP access, or both.
    credits_granted = models.IntegerField(default=0)
    unlimited = models.BooleanField(default=False)
    includes_vip_slips = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_usd"]

    def __str__(self) -> str:
        return f"{self.name} — ${self.price_usd}"


class Subscription(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_current(self) -> bool:
        return self.status == self.Status.ACTIVE and self.expires_at > timezone.now()


class Wallet(TimeStampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.IntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.user} — {self.balance} credits"

    @transaction.atomic
    def grant(self, amount: int, reason: str, ref: str = "") -> "CreditEntry":
        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])
        self.balance = wallet.balance
        return CreditEntry.objects.create(
            wallet=wallet, amount=amount, reason=reason, ref=ref, balance_after=wallet.balance
        )

    @transaction.atomic
    def spend(self, amount: int, reason: str, ref: str = "") -> "CreditEntry | None":
        """Returns None when the balance can't cover it — caller shows the upsell."""
        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        if wallet.balance < amount:
            return None
        wallet.balance -= amount
        wallet.save(update_fields=["balance", "updated_at"])
        self.balance = wallet.balance
        return CreditEntry.objects.create(
            wallet=wallet, amount=-amount, reason=reason, ref=ref, balance_after=wallet.balance
        )


class CreditEntry(TimeStampedModel):
    class Reason(models.TextChoices):
        SIGNUP = "signup", "Signup bonus"
        REFERRAL = "referral", "Referral bonus"
        PURCHASE = "purchase", "Plan purchase"
        UNLOCK = "unlock", "Prediction unlock"
        ADMIN = "admin", "Manual adjustment"
        REFUND = "refund", "Refund"

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="entries")
    amount = models.IntegerField(help_text="Positive grants, negative spends")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    ref = models.CharField(max_length=128, blank=True)
    balance_after = models.IntegerField()

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "credit entries"


class Payment(TimeStampedModel):
    """
    A crypto invoice. NOWPayments is the primary rail, Cryptomus the fallback —
    both are webhook-settled, so `status` only ever advances on a signed IPN, never
    on the user saying they paid.
    """

    class Provider(models.TextChoices):
        NOWPAYMENTS = "nowpayments", "NOWPayments"
        CRYPTOMUS = "cryptomus", "Cryptomus"
        MANUAL = "manual", "Manual / admin"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMING = "confirming", "Confirming"
        PAID = "paid", "Paid"
        PARTIAL = "partial", "Partially paid"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"
        REFUNDED = "refunded", "Refunded"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(
        max_length=16, choices=Provider.choices, default=Provider.NOWPAYMENTS
    )
    provider_ref = models.CharField(max_length=128, blank=True, db_index=True)
    invoice_url = models.URLField(blank=True)

    amount_usd = models.DecimalField(max_digits=10, decimal_places=2)
    pay_currency = models.CharField(max_length=16, blank=True, help_text="usdttrc20, btc …")
    pay_amount = models.DecimalField(max_digits=24, decimal_places=10, default=Decimal("0"))

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Full IPN body kept verbatim — the only defence in a chargeback-style dispute.
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} — {self.plan.code} — {self.status}"
