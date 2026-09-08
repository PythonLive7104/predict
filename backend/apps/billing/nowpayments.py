"""NOWPayments invoice creation. Settlement happens in the IPN, never here."""

import httpx
from django.conf import settings

from .models import Payment


class NowPaymentsError(RuntimeError):
    pass


def create_invoice(payment: Payment, success_url: str = "", cancel_url: str = "") -> Payment:
    """
    Creates a hosted invoice the user pays in any supported coin. `order_id` is
    our Payment pk so the IPN can find its way home.
    """
    if not settings.NOWPAYMENTS_API_KEY:
        raise NowPaymentsError("NOWPAYMENTS_API_KEY is not set")

    body = {
        "price_amount": float(payment.amount_usd),
        "price_currency": "usd",
        "order_id": str(payment.pk),
        "order_description": f"{payment.plan.name} — {payment.plan.duration_days} days",
        "ipn_callback_url": f"{settings.FRONTEND_URL.rstrip('/')}/hooks/nowpayments/",
        "success_url": success_url,
        "cancel_url": cancel_url,
    }

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"{settings.NOWPAYMENTS_BASE_URL}/invoice",
            headers={"x-api-key": settings.NOWPAYMENTS_API_KEY},
            json=body,
        )
    resp.raise_for_status()
    data = resp.json()

    payment.provider_ref = str(data.get("id", ""))
    payment.invoice_url = data.get("invoice_url", "")
    payment.save(update_fields=["provider_ref", "invoice_url", "updated_at"])
    return payment
