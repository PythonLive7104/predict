"""Auth for both front doors: Telegram Mini App initData, and the user's own profile."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.predictions.access import entitlement_for

from . import services
from .auth import InitDataError, verify_init_data


@api_view(["POST"])
@permission_classes([AllowAny])
def telegram_login(request):
    """
    Exchange a signed Mini App `initData` string for a JWT pair.

    This is what lets one React build serve both surfaces: inside Telegram the
    page posts initData here and gets the same tokens the public web app uses.
    """
    init_data = request.data.get("init_data", "")
    if not init_data:
        return Response({"detail": "init_data is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        payload = verify_init_data(init_data)
    except InitDataError as exc:
        # Deliberately vague: a precise reason tells a forger which knob to turn.
        return Response({"detail": "Invalid Telegram data."}, status=status.HTTP_401_UNAUTHORIZED)

    tg_user = payload.get("user") or {}
    if not tg_user.get("id"):
        return Response({"detail": "Invalid Telegram data."}, status=status.HTTP_401_UNAUTHORIZED)

    user, _ = services.get_or_create_from_telegram(
        telegram_id=tg_user["id"],
        username=tg_user.get("username", ""),
        language_code=tg_user.get("language_code", "en"),
        referral_code=payload.get("start_param", ""),
    )

    refresh = RefreshToken.for_user(user)
    return Response({
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": _profile(user),
    })


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def me(request):
    """PATCH accepts only `notifications_enabled` — the rest of the profile is
    derived from subscriptions and must never be client-settable."""
    if request.method == "PATCH":
        enabled = request.data.get("notifications_enabled")
        if not isinstance(enabled, bool):
            return Response(
                {"detail": "notifications_enabled must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.notifications_enabled = enabled
        request.user.save(update_fields=["notifications_enabled"])

    return Response(_profile(request.user))


def _profile(user) -> dict:
    e = entitlement_for(user)
    return {
        "id": user.pk,
        "username": user.telegram_username or user.username,
        "plan": e.plan_name,
        "credits": e.credits,
        "unlimited": e.unlimited,
        "vip": e.vip,
        "expires_at": e.expires_at,
        "referral_code": user.referral_code,
        "notifications_enabled": user.notifications_enabled,
    }
