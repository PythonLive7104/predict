"""
Read API for the SPA and the Mini App.

Every entitlement question routes through apps.predictions.access, so these views
hold no rules of their own — they translate HTTP to domain calls and back.
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import access
from .models import PredictionView
from .serializers import PredictionSerializer, SlipSerializer


def _unlocked_ids(user, predictions) -> set[int]:
    """One query for the whole page rather than one per card."""
    if not user or not user.is_authenticated:
        return set()
    if access.entitlement_for(user).unlimited:
        return {p.pk for p in predictions}
    return set(
        PredictionView.objects.filter(
            user=user, prediction__in=predictions
        ).values_list("prediction_id", flat=True)
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def todays_picks(request):
    """
    Public: anyone can see the slate and the confidence. The selection itself is
    stripped for locked picks — that's the product.
    """
    on = request.query_params.get("date")
    parsed = timezone.datetime.fromisoformat(on).date() if on else None
    picks = list(access.published_picks(on=parsed))
    unlocked = _unlocked_ids(request.user, picks)

    data = [
        PredictionSerializer(p, context={"unlocked": p.pk in unlocked}).data for p in picks
    ]
    return Response({"date": (parsed or timezone.now().date()), "count": len(data), "results": data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def unlock_pick(request, pk: int):
    prediction, result = access.unlock(request.user, pk)

    if result == "missing":
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if result == "insufficient":
        return Response(
            {"detail": "Not enough credits.", "code": "insufficient_credits",
             "credits": access.entitlement_for(request.user).credits},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )

    return Response({
        "status": result,
        "credits": access.entitlement_for(request.user).credits,
        "prediction": PredictionSerializer(prediction, context={"unlocked": True}).data,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def todays_slips(request):
    """Slips are VIP; non-subscribers get the shape but not the legs."""
    slips = list(access.slips_for())
    vip = bool(
        request.user.is_authenticated and access.entitlement_for(request.user).vip
    )
    data = SlipSerializer(slips, many=True, context={"unlocked": vip}).data
    return Response({"vip": vip, "results": data})


@api_view(["GET"])
@permission_classes([AllowAny])
def record(request):
    """
    The public accuracy record — deliberately unauthenticated. It is the strongest
    sales asset the product has, and hiding it behind login would defeat it.
    """
    days = int(request.query_params.get("days", 30))
    return Response({
        "summary": access.record_summary(days),
        "by_market": access.record_by_market(days),
    })
