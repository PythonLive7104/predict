"""
Unauthenticated endpoints. Neither is protected by a session — each verifies the
caller cryptographically instead, and anything that fails verification is dropped
silently (a 200 with no side effect) so probes learn nothing.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


@csrf_exempt
def telegram_webhook(request):
    """Telegram calls this with the secret token it was registered with."""
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(
        token, settings.TELEGRAM_WEBHOOK_SECRET
    ):
        logger.warning("telegram webhook: bad secret token")
        return HttpResponse(status=403)

    update = json.loads(request.body or b"{}")
    # Handing straight to Celery keeps Telegram's 60s retry window irrelevant —
    # the bot answers instantly and the work happens off-request.
    from .tasks import process_update

    process_update.delay(update)
    return JsonResponse({"ok": True})


@csrf_exempt
def nowpayments_ipn(request):
    """
    NOWPayments signs the IPN body with HMAC-SHA512 over the JSON with keys
    sorted. A payment is only ever credited from here — never from the client.
    """
    signature = request.headers.get("x-nowpayments-sig", "")
    body = request.body or b"{}"
    expected = hmac.new(
        settings.NOWPAYMENTS_IPN_SECRET.encode(),
        json.dumps(json.loads(body), separators=(",", ":"), sort_keys=True).encode(),
        hashlib.sha512,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        logger.warning("nowpayments ipn: signature mismatch")
        return HttpResponse(status=403)

    from apps.billing.tasks import settle_payment

    settle_payment.delay(json.loads(body))
    return JsonResponse({"ok": True})
