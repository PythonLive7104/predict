from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.billing.models import CreditEntry, Wallet

User = get_user_model()


class WalletTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="tg_1", telegram_id=1)
        self.wallet = Wallet.objects.create(user=self.user)

    def test_grant_increases_balance_and_writes_a_ledger_entry(self):
        entry = self.wallet.grant(10, CreditEntry.Reason.SIGNUP)
        self.wallet.refresh_from_db()

        self.assertEqual(self.wallet.balance, 10)
        self.assertEqual(entry.amount, 10)
        self.assertEqual(entry.balance_after, 10)

    def test_spend_deducts_and_records_a_negative_entry(self):
        self.wallet.grant(10, CreditEntry.Reason.SIGNUP)
        entry = self.wallet.spend(3, CreditEntry.Reason.UNLOCK)
        self.wallet.refresh_from_db()

        self.assertEqual(self.wallet.balance, 7)
        self.assertEqual(entry.amount, -3)
        self.assertEqual(entry.balance_after, 7)

    def test_overspend_is_refused_and_leaves_no_trace(self):
        self.wallet.grant(2, CreditEntry.Reason.SIGNUP)
        self.assertIsNone(self.wallet.spend(5, CreditEntry.Reason.UNLOCK))

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 2)
        self.assertEqual(self.wallet.entries.filter(reason=CreditEntry.Reason.UNLOCK).count(), 0)

    def test_spending_the_exact_balance_is_allowed(self):
        self.wallet.grant(5, CreditEntry.Reason.SIGNUP)
        self.assertIsNotNone(self.wallet.spend(5, CreditEntry.Reason.UNLOCK))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 0)

    def test_ledger_reconstructs_the_balance(self):
        """balance is a cache; the entries are the truth. They must agree."""
        self.wallet.grant(10, CreditEntry.Reason.SIGNUP)
        self.wallet.grant(5, CreditEntry.Reason.REFERRAL)
        self.wallet.spend(3, CreditEntry.Reason.UNLOCK)
        self.wallet.spend(1, CreditEntry.Reason.UNLOCK)

        self.wallet.refresh_from_db()
        ledger_total = sum(e.amount for e in self.wallet.entries.all())
        self.assertEqual(self.wallet.balance, ledger_total)
        self.assertEqual(self.wallet.balance, 11)
