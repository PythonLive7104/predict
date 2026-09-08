from django.contrib import admin

from .models import ReceivingWallet, CreditEntry, Payment, Plan, Subscription, Wallet


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price_usd", "duration_days", "unlimited", "is_active")
    list_editable = ("is_active",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "amount_usd", "provider", "status", "paid_at")
    list_filter = ("provider", "status")
    search_fields = ("provider_ref", "user__telegram_username")
    readonly_fields = ("raw",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "status", "starts_at", "expires_at")
    list_filter = ("status", "plan")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance")
    search_fields = ("user__telegram_username",)


@admin.register(CreditEntry)
class CreditEntryAdmin(admin.ModelAdmin):
    list_display = ("wallet", "amount", "reason", "balance_after", "created_at")
    list_filter = ("reason",)


@admin.register(ReceivingWallet)
class ReceivingWalletAdmin(admin.ModelAdmin):
    """The owner edits these; no redeploy needed to rotate an address."""

    list_display = ("label", "currency", "network", "address", "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")
    search_fields = ("label", "address")
