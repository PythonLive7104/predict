"""
Telegram surface state.

The bot is a thin client: it holds conversation state and delivery receipts, and
every domain decision (can this user see this pick? what does this plan cost?)
lives in predictions/billing so the phase-3 web app and Mini App reach the same
answer.
"""

from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel
from apps.predictions.models import Prediction, Slip


class BotSession(TimeStampedModel):
    """FSM scratch space — which flow the user is in, and what they last touched."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bot_session"
    )
    chat_id = models.BigIntegerField(db_index=True)
    state = models.CharField(max_length=64, blank=True)
    context = models.JSONField(default=dict, blank=True)
    last_command = models.CharField(max_length=64, blank=True)


class Broadcast(TimeStampedModel):
    """Admin push — a new slip dropping, or a marketing blast."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    title = models.CharField(max_length=128)
    body = models.TextField()
    slip = models.ForeignKey(
        Slip, null=True, blank=True, on_delete=models.SET_NULL, related_name="broadcasts"
    )
    # Empty targets everyone; otherwise restrict to a tier.
    target_tier = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)


class Delivery(TimeStampedModel):
    """
    Receipt per (user, thing sent). Unique-constrained so a retried Celery task
    can't double-send — the fastest way to get a bot reported for spam.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deliveries"
    )
    broadcast = models.ForeignKey(
        Broadcast, null=True, blank=True, on_delete=models.CASCADE, related_name="deliveries"
    )
    prediction = models.ForeignKey(
        Prediction, null=True, blank=True, on_delete=models.CASCADE, related_name="deliveries"
    )
    message_id = models.BigIntegerField(null=True, blank=True)
    delivered = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "deliveries"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "broadcast"],
                condition=models.Q(broadcast__isnull=False),
                name="uniq_broadcast_delivery",
            ),
            models.UniqueConstraint(
                fields=["user", "prediction"],
                condition=models.Q(prediction__isnull=False),
                name="uniq_prediction_delivery",
            ),
        ]
