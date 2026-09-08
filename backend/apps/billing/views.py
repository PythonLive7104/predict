"""Plans, checkout, and the user's own credit history."""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Payment, Plan, Wallet
from .nowpayments import create_invoice
from .serializers import CreditEntrySerializer, PaymentSerializer, PlanSerializer

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([AllowAny])
def plans(request):
    return Response(PlanSerializer(Plan.objects.filter(is_active=True), many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def checkout(request):
    """
    Opens a crypto invoice. Grants nothing — the plan is only ever activated by
    the signed IPN, so a user who closes this page mid-payment still gets it.
    """
    plan = Plan.objects.filter(code=request.data.get("plan"), is_active=True).first()
    if plan is None:
        return Response({"detail": "Unknown plan."}, status=status.HTTP_404_NOT_FOUND)

    payment = Payment.objects.create(
        user=request.user, plan=plan, amount_usd=plan.price_usd
    )
    try:
        payment = create_invoice(payment)
    except Exception:
        logger.exception("invoice creation failed for payment %s", payment.pk)
        return Response(
            {"detail": "Could not open a payment window. Please try again."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def credit_history(request):
    wallet, _ = Wallet.objects.get_or_create(user=request.user)
    return Response({
        "balance": wallet.balance,
        "entries": CreditEntrySerializer(wallet.entries.all()[:50], many=True).data,
    })
