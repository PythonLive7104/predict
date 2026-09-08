from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.predictions.urls")),
    path("api/", include("apps.billing.urls")),
    # Telegram webhook + crypto IPN callbacks (both unauthenticated, both verified
    # by provider signature rather than by session).
    path("hooks/", include("apps.bot.urls")),
]
