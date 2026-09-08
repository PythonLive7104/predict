"""
Domain calls the handlers need, wrapped for async use.

Handlers stay presentation-only: everything that decides *what a user is
entitled to* lives here and in the billing app, so the phase-3 web app and Mini
App get the same answers without reimplementing the rules.
"""

from datetime import date

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts import services as accounts_services
from apps.accounts.models import User
from apps.billing.models import CreditEntry, Plan, Wallet
from apps.predictions import access
from apps.predictions.models import Prediction, Tier


@sync_to_async
def get_or_create_user(tg_user, referral_code: str = "") -> tuple[User, bool]:
    user, created = accounts_services.get_or_create_from_telegram(
        telegram_id=tg_user.id,
        username=tg_user.username or "",
        language_code=tg_user.language_code or "en",
        referral_code=referral_code,
    )
    user.last_seen_at = timezone.now()
    user.save(update_fields=["last_seen_at"])
    return user, created


@sync_to_async
def plan_summary(user: User) -> dict:
    e = access.entitlement_for(user)
    return {
        "plan_name": e.plan_name, "expires_at": e.expires_at,
        "unlimited": e.unlimited, "credits": e.credits, "vip": e.vip,
    }


@sync_to_async
def todays_free_picks(limit: int = 5) -> list[Prediction]:
    return list(access.published_picks(tier=Tier.FREE).order_by("-confidence")[:limit])


@sync_to_async
def vip_slip_for_today():
    return access.slips_for().first()


@sync_to_async
def unlock_prediction(user: User, prediction_id: int, cost: int = 1):
    return access.unlock(user, prediction_id, cost)


@sync_to_async
def active_plans() -> list[Plan]:
    return list(Plan.objects.filter(is_active=True))


@sync_to_async
def get_plan(code: str) -> Plan | None:
    return Plan.objects.filter(code=code, is_active=True).first()


@sync_to_async
def record_summary(days: int = 30) -> dict:
    return access.record_summary(days)


@sync_to_async
def set_notifications(user: User, enabled: bool) -> bool:
    user.notifications_enabled = enabled
    user.save(update_fields=["notifications_enabled"])
    return enabled


# --- Manual crypto rail ---------------------------------------------------

@sync_to_async
def receiving_wallets() -> list:
    from apps.billing.manual import active_wallets

    return active_wallets()


@sync_to_async
def open_manual_payment(user: User, plan):
    from apps.billing.manual import open_payment

    return open_payment(user, plan)


@sync_to_async
def submit_tx_hash(user: User, tx_hash: str) -> tuple:
    from apps.billing.manual import submit_hash

    payment, status = submit_hash(user, tx_hash)
    if payment is None:
        return None, status
    # Flattened here because the handler is async and must not touch the ORM:
    # every related field it needs is resolved while we are still in a thread.
    return {
        "id": payment.pk,
        "plan": payment.plan.name,
        "amount": str(payment.amount_usd),
        "tx_hash": payment.tx_hash,
        "user_id": payment.user.telegram_id,
        "username": payment.user.telegram_username or "",
    }, status


@sync_to_async
def decide_payment(payment_id: int, admin_id: int, approved: bool) -> tuple:
    from apps.billing.manual import approve, reject

    payment, status = (approve if approved else reject)(payment_id, admin_id)
    if payment is None:
        return None, status
    return {
        "id": payment.pk,
        "plan": payment.plan.name,
        "amount": str(payment.amount_usd),
        "user_id": payment.user.telegram_id,
        "username": payment.user.telegram_username or "",
    }, status


@sync_to_async
def pending_review_count() -> int:
    from apps.billing.models import Payment

    return Payment.objects.filter(status=Payment.Status.REVIEW).count()
