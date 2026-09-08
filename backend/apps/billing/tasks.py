"""Payment settlement. Only a verified IPN reaches these functions."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import CreditEntry, Payment, Subscription, Wallet

logger = logging.getLogger(__name__)

# NOWPayments statuses that mean the money actually arrived.
PAID_STATUSES = {"finished", "confirmed"}


def _find_payment(ipn: dict) -> Payment | None:
    """
    Resolve the IPN to our Payment row: by `order_id` (our pk, which we set when
    creating the invoice), falling back to the provider's own reference.

    Both lookups must be guarded against empty values. `provider_ref` is blank
    until an invoice is created, so filtering on an empty string would match an
    arbitrary un-invoiced payment and credit the wrong user.
    """
    order_id = str(ipn.get("order_id") or "").strip()
    if order_id.isdigit():
        payment = Payment.objects.filter(pk=int(order_id)).first()
        if payment is not None:
            return payment

    provider_ref = str(ipn.get("payment_id") or "").strip()
    if provider_ref:
        return Payment.objects.filter(provider_ref=provider_ref).first()

    return None


@shared_task
def settle_payment(ipn: dict) -> bool:
    """
    Idempotent: a provider that retries the IPN (they all do) must not grant the
    plan twice. The Payment row's own status is the lock.
    """
    payment = _find_payment(ipn)

    if payment is None:
        logger.warning("ipn for unknown payment: %s", ipn)
        return False

    if payment.status == Payment.Status.PAID:
        return False  # already granted

    status = (ipn.get("payment_status") or "").lower()
    payment.raw = ipn
    payment.provider_ref = str(ipn.get("payment_id", "")) or payment.provider_ref
    payment.pay_currency = ipn.get("pay_currency", "") or payment.pay_currency

    if status not in PAID_STATUSES:
        payment.status = {
            "waiting": Payment.Status.PENDING,
            "confirming": Payment.Status.CONFIRMING,
            "partially_paid": Payment.Status.PARTIAL,
            "failed": Payment.Status.FAILED,
            "expired": Payment.Status.EXPIRED,
            "refunded": Payment.Status.REFUNDED,
        }.get(status, payment.status)
        payment.save()
        return False

    payment.status = Payment.Status.PAID
    payment.paid_at = timezone.now()
    payment.save()

    grant_plan(payment)
    return True


def grant_plan(payment: Payment) -> Subscription:
    """Extend from the current expiry, not from now — renewing early loses nothing."""
    plan, user = payment.plan, payment.user

    current = (
        Subscription.objects.filter(user=user, status=Subscription.Status.ACTIVE)
        .order_by("-expires_at")
        .first()
    )
    starts = current.expires_at if current and current.is_current else timezone.now()

    subscription = Subscription.objects.create(
        user=user,
        plan=plan,
        starts_at=starts,
        expires_at=starts + timedelta(days=plan.duration_days),
    )

    if plan.credits_granted:
        wallet, _ = Wallet.objects.get_or_create(user=user)
        wallet.grant(
            plan.credits_granted, CreditEntry.Reason.PURCHASE, ref=f"payment:{payment.pk}"
        )

    logger.info("granted %s to %s until %s", plan.code, user, subscription.expires_at)
    return subscription


@shared_task
def expire_subscriptions() -> int:
    """Nightly sweep so `is_current` and the stored status can't drift apart."""
    return Subscription.objects.filter(
        status=Subscription.Status.ACTIVE, expires_at__lt=timezone.now()
    ).update(status=Subscription.Status.EXPIRED)
