from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Payment, Plan, Subscription, Wallet
from apps.billing.tasks import expire_subscriptions, grant_plan, settle_payment

User = get_user_model()


class PaymentSettlementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="tg_2", telegram_id=2)
        self.plan = Plan.objects.create(
            code="pro-1m", name="Pro", price_usd=Decimal("60.00"),
            duration_days=30, credits_granted=50,
        )
        self.payment = Payment.objects.create(
            user=self.user, plan=self.plan, amount_usd=self.plan.price_usd
        )

    def _ipn(self, status="finished"):
        return {
            "order_id": str(self.payment.pk),
            "payment_id": 12345,
            "payment_status": status,
            "pay_currency": "usdttrc20",
        }

    def test_finished_ipn_grants_the_plan(self):
        self.assertTrue(settle_payment(self._ipn()))

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PAID)
        self.assertTrue(Subscription.objects.filter(user=self.user).exists())
        self.assertEqual(Wallet.objects.get(user=self.user).balance, 50)

    def test_repeated_ipn_does_not_grant_twice(self):
        """Every provider retries IPNs. Granting twice is money out the door."""
        self.assertTrue(settle_payment(self._ipn()))
        self.assertFalse(settle_payment(self._ipn()))

        self.assertEqual(Subscription.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Wallet.objects.get(user=self.user).balance, 50)

    def test_unconfirmed_statuses_grant_nothing(self):
        for status in ("waiting", "confirming", "partially_paid", "failed", "expired"):
            with self.subTest(status=status):
                self.assertFalse(settle_payment(self._ipn(status)))

        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_unknown_order_is_rejected(self):
        self.assertFalse(settle_payment({"order_id": "999999", "payment_status": "finished"}))

    def test_renewing_early_extends_from_the_current_expiry(self):
        """Paying before you run out must not burn the remaining days."""
        first = grant_plan(self.payment)

        second_payment = Payment.objects.create(
            user=self.user, plan=self.plan, amount_usd=self.plan.price_usd
        )
        second = grant_plan(second_payment)

        self.assertEqual(second.starts_at, first.expires_at)
        self.assertAlmostEqual(
            (second.expires_at - timezone.now()).days, 59, delta=1
        )


class ExpiryTests(TestCase):
    def test_lapsed_subscriptions_are_marked_expired(self):
        user = User.objects.create(username="tg_3", telegram_id=3)
        plan = Plan.objects.create(
            code="p", name="P", price_usd=Decimal("1"), duration_days=30
        )
        stale = Subscription.objects.create(
            user=user, plan=plan,
            starts_at=timezone.now() - timedelta(days=60),
            expires_at=timezone.now() - timedelta(days=30),
        )
        live = Subscription.objects.create(
            user=user, plan=plan, expires_at=timezone.now() + timedelta(days=10)
        )

        self.assertEqual(expire_subscriptions(), 1)

        stale.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(stale.status, Subscription.Status.EXPIRED)
        self.assertEqual(live.status, Subscription.Status.ACTIVE)
        self.assertTrue(live.is_current)
