from django.urls import path

from . import webhooks

app_name = "hooks"

urlpatterns = [
    path("telegram/", webhooks.telegram_webhook, name="telegram"),
    path("nowpayments/", webhooks.nowpayments_ipn, name="nowpayments"),
]
