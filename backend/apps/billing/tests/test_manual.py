"""
Manual crypto rail.

A transaction hash is public — anyone can read one off a block explorer. What
stops that becoming a free-plan generator is the uniqueness constraint and the
rule that approval, not submission, grants anything. Both are pinned here.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.billing import manual
from apps.billing.models import Payment, Plan, ReceivingWallet, Subscription, Wallet

User = get_user_model()


def _user(tid: int) -> User:
    user = User.objects.create(username=f"tg_{tid}", telegram_id=tid)
    Wallet.objects.create(user=user)
    return user


def _plan() -> Plan:
    return Plan.objects.create(
        code="pro-1m", name="Pro · 1 month", price_usd=Decimal("60"),
        duration_days=30, credits_granted=50, includes_vip_slips=True,
    )


class SubmissionTests(TestCase):
    def setUp(self):
        self.user = _user(111)
        self.plan = _plan()
        ReceivingWallet.objects.create(
            label="USDT (TRC-20)", currency="usdt", network="TRC20", address="TXyz123"
        )

    def test_submitting_queues_for_review_and_grants_nothing(self):
        """The whole point: paying is a claim, not an entitlement."""
        manual.open_payment(self.user, self.plan)
        payment, status = manual.submit_hash(self.user, "a" * 64)

        self.assertEqual(status, "submitted")
        row = Payment.objects.get(pk=payment.pk)
        self.assertEqual(row.status, Payment.Status.REVIEW)
        self.assertIsNotNone(row.submitted_at)
        self.assertFalse(Subscription.objects.exists())
        self.assertEqual(self.user.wallet.balance, 0)

    def test_a_hash_already_used_by_someone_else_is_refused(self):
        """Lift a hash off a block explorer, paste it, get nothing."""
        manual.open_payment(self.user, self.plan)
        manual.submit_hash(self.user, "shared-hash-0000000000")

        thief = _user(222)
        manual.open_payment(thief, self.plan)
        payment, status = manual.submit_hash(thief, "shared-hash-0000000000")

        self.assertEqual(status, "duplicate")
        self.assertIsNone(payment)
        self.assertEqual(
            Payment.objects.filter(status=Payment.Status.REVIEW).count(), 1
        )

    def test_resubmitting_your_own_hash_is_a_no_op(self):
        manual.open_payment(self.user, self.plan)
        manual.submit_hash(self.user, "b" * 64)
        payment, status = manual.submit_hash(self.user, "b" * 64)

        self.assertEqual(status, "resubmit")
        self.assertIsNone(payment)

    def test_a_hash_with_no_open_payment_is_ignored(self):
        """Otherwise a stray code pasted in chat becomes a payment claim."""
        payment, status = manual.submit_hash(self.user, "c" * 64)
        self.assertEqual(status, "no_payment")
        self.assertIsNone(payment)

    def test_tapping_a_plan_repeatedly_does_not_stack_payments(self):
        for _ in range(3):
            manual.open_payment(self.user, self.plan)
        self.assertEqual(Payment.objects.filter(user=self.user).count(), 1)

    def test_hash_shape_filter_ignores_chat_text(self):
        self.assertTrue(manual.looks_like_tx_hash("0x" + "a" * 64))
        self.assertTrue(manual.looks_like_tx_hash("A" * 64))
        self.assertFalse(manual.looks_like_tx_hash("thanks!"))
        self.assertFalse(manual.looks_like_tx_hash("i have paid already"))
        self.assertFalse(manual.looks_like_tx_hash("short"))


class ApprovalTests(TestCase):
    def setUp(self):
        self.user = _user(111)
        self.plan = _plan()
        manual.open_payment(self.user, self.plan)
        self.payment, _ = manual.submit_hash(self.user, "d" * 64)

    def test_approval_grants_the_plan(self):
        payment, status = manual.approve(self.payment.pk, admin_telegram_id=999)

        self.assertEqual(status, "approved")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(payment.reviewed_by, 999)
        self.assertIsNotNone(payment.paid_at)

        sub = Subscription.objects.get(user=self.user)
        self.assertEqual(sub.plan, self.plan)
        self.assertTrue(sub.is_current)
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, 50)

    def test_approving_twice_does_not_grant_twice(self):
        """Two admins can open the same notification and both tap Approve."""
        manual.approve(self.payment.pk, 999)
        payment, status = manual.approve(self.payment.pk, 888)

        self.assertEqual(status, "already_approved")
        self.assertEqual(Subscription.objects.count(), 1)
        self.user.wallet.refresh_from_db()
        self.assertEqual(self.user.wallet.balance, 50)

    def test_rejection_grants_nothing(self):
        payment, status = manual.reject(self.payment.pk, 999, "amount didn't match")

        self.assertEqual(status, "rejected")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.REJECTED)
        self.assertEqual(payment.review_note, "amount didn't match")
        self.assertFalse(Subscription.objects.exists())

    def test_a_granted_plan_is_never_silently_revoked(self):
        """Reversing a payment is a refund decision, not a status flip."""
        manual.approve(self.payment.pk, 999)
        payment, status = manual.reject(self.payment.pk, 999, "changed my mind")

        self.assertEqual(status, "already_approved")
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertTrue(Subscription.objects.filter(user=self.user).exists())

    def test_a_rejected_hash_can_be_corrected_and_resubmitted(self):
        manual.reject(self.payment.pk, 999, "wrong network")
        manual.open_payment(self.user, self.plan)
        payment, status = manual.submit_hash(self.user, "e" * 64)
        self.assertEqual(status, "submitted")

    def test_missing_payment_is_handled(self):
        payment, status = manual.approve(999999, 999)
        self.assertEqual(status, "missing")
        self.assertIsNone(payment)
