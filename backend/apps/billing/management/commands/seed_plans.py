"""Seed the plan tiers the bot renders under Purchase."""

from django.core.management.base import BaseCommand

from apps.billing.models import Plan

PLANS = [
    {
        "code": "starter-1m", "name": "Starter · 1 month", "price_usd": "25.00",
        "duration_days": 30, "credits_granted": 100, "unlimited": False,
        "includes_vip_slips": False, "sort_order": 1,
        "description": "100 pick unlocks. Free picks stay free.",
    },
    {
        "code": "pro-1m", "name": "Pro · 1 month", "price_usd": "60.00",
        "duration_days": 30, "credits_granted": 0, "unlimited": True,
        "includes_vip_slips": True, "sort_order": 2,
        "description": "Unlimited picks plus the daily VIP slip.",
    },
    {
        "code": "pro-6m", "name": "Pro · 6 months", "price_usd": "250.00",
        "duration_days": 180, "credits_granted": 0, "unlimited": True,
        "includes_vip_slips": True, "sort_order": 3,
        "description": "Unlimited picks and VIP slips for six months.",
    },
]


class Command(BaseCommand):
    help = "Create or update the default plan tiers"

    def handle(self, *args, **options):
        for spec in PLANS:
            plan, created = Plan.objects.update_or_create(
                code=spec.pop("code"), defaults=spec
            )
            verb = "created" if created else "updated"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {plan.name}"))
