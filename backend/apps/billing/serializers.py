from rest_framework import serializers

from .models import CreditEntry, Payment, Plan


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = (
            "code", "name", "description", "price_usd", "duration_days",
            "credits_granted", "unlimited", "includes_vip_slips",
        )


class PaymentSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)

    class Meta:
        model = Payment
        fields = ("id", "plan", "amount_usd", "provider", "status", "invoice_url", "created_at")


class CreditEntrySerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = CreditEntry
        fields = ("amount", "reason", "reason_label", "balance_after", "created_at")
