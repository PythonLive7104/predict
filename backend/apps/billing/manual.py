"""
Manual crypto rail: the owner publishes a wallet, the user pays it directly and
submits the transaction hash, an admin verifies and approves.

The rule the IPN rail enforces with a signature is enforced here by a human:
**a plan is granted only on approval, never because the user said they paid.**
Everything below exists to keep that true under the ways it can be attacked —
a hash copied off a block explorer, a hash submitted twice, two admins tapping
approve at the same moment.
"""

import logging
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Payment, Plan, ReceivingWallet
from .tasks import grant_plan

logger = logging.getLogger(__name__)

# Deliberately loose: hashes differ by chain (BTC is 64 hex, TRON 64 hex, some
# chains prefix 0x) and a rejected-but-valid hash is a support ticket. This only
# filters out obvious chat text so a "thanks!" isn't read as a submission.
TX_HASH_RE = re.compile(r"^(0x)?[A-Za-z0-9]{16,128}$")


def looks_like_tx_hash(text: str) -> bool:
    return bool(TX_HASH_RE.match(text.strip()))


def active_wallets() -> list[ReceivingWallet]:
    return list(ReceivingWallet.objects.filter(is_active=True))


@transaction.atomic
def open_payment(user, plan: Plan) -> Payment:
    """
    Start a manual payment when the user picks a plan.

    Reuses the user's existing open attempt for the same plan rather than
    stacking rows — a user tapping a plan three times while deciding should not
    leave three pending payments for an admin to reconcile.
    """
    existing = (
        Payment.objects.select_for_update()
        .filter(
            user=user, plan=plan,
            provider=Payment.Provider.MANUAL,
            status=Payment.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing

    return Payment.objects.create(
        user=user, plan=plan,
        provider=Payment.Provider.MANUAL,
        amount_usd=plan.price_usd,
        status=Payment.Status.PENDING,
    )


@transaction.atomic
def submit_hash(user, tx_hash: str, wallet_address: str = "") -> tuple[Payment | None, str]:
    """
    Attach a hash to the user's open payment and put it in the review queue.

    Returns (payment, status) where status is one of:
      submitted  — queued for review
      duplicate  — that hash is already on another payment
      no_payment — the user has no plan awaiting payment
      resubmit   — they already submitted; nothing to do but wait
    """
    tx_hash = tx_hash.strip()

    # Checked before claiming anything so a duplicate never disturbs a live row.
    # Covers the obvious attack: lift a hash off a block explorer and paste it.
    if Payment.objects.filter(tx_hash=tx_hash).exclude(user=user).exists():
        logger.warning("duplicate tx hash from user %s: %s", user.pk, tx_hash)
        return None, "duplicate"

    mine = Payment.objects.select_for_update().filter(
        user=user, provider=Payment.Provider.MANUAL
    )
    if mine.filter(tx_hash=tx_hash).exclude(status=Payment.Status.REJECTED).exists():
        return None, "resubmit"

    payment = mine.filter(status=Payment.Status.PENDING).order_by("-created_at").first()
    if payment is None:
        return None, "no_payment"

    payment.tx_hash = tx_hash
    payment.wallet_address = wallet_address or payment.wallet_address
    payment.status = Payment.Status.REVIEW
    payment.submitted_at = timezone.now()
    try:
        payment.save()
    except IntegrityError:
        # The unique constraint caught a race the pre-check could not.
        logger.warning("tx hash race for user %s: %s", user.pk, tx_hash)
        return None, "duplicate"

    return payment, "submitted"


@transaction.atomic
def approve(payment_id: int, admin_telegram_id: int) -> tuple[Payment | None, str]:
    """
    Grant the plan. Idempotent under two admins tapping Approve at once —
    `select_for_update` serialises them and the status check stops the second.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("user", "plan")
        .filter(pk=payment_id)
        .first()
    )
    if payment is None:
        return None, "missing"
    if payment.status == Payment.Status.PAID:
        return payment, "already_approved"

    payment.status = Payment.Status.PAID
    payment.paid_at = timezone.now()
    payment.reviewed_at = timezone.now()
    payment.reviewed_by = admin_telegram_id
    payment.save()

    grant_plan(payment)
    logger.info("payment %s approved by admin %s", payment.pk, admin_telegram_id)
    return payment, "approved"


@transaction.atomic
def reject(payment_id: int, admin_telegram_id: int, note: str = "") -> tuple[Payment | None, str]:
    payment = (
        Payment.objects.select_for_update()
        .select_related("user", "plan")
        .filter(pk=payment_id)
        .first()
    )
    if payment is None:
        return None, "missing"
    if payment.status == Payment.Status.PAID:
        # Refunds are a decision, not a status flip — never silently undo a grant.
        return payment, "already_approved"

    payment.status = Payment.Status.REJECTED
    payment.reviewed_at = timezone.now()
    payment.reviewed_by = admin_telegram_id
    payment.review_note = note[:255]
    payment.save()
    logger.info("payment %s rejected by admin %s", payment.pk, admin_telegram_id)
    return payment, "rejected"
