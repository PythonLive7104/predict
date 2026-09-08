"""
Predictions — the hybrid engine's output and its public record.

Pipeline per fixture (see engine/pipeline.py):
  1. Build a stat pack from apps.fixtures (form, Elo, attack/defence, H2H,
     injuries, market odds).
  2. Statistical layer produces calibrated probabilities per market
     (Poisson goal model + Elo prior).
  3. Claude reads the same pack and writes the user-facing rationale, and may
     flag context the numbers miss (derby, dead rubber, keeper out).
  4. Anything under MIN_PUBLISH_CONFIDENCE is stored but never published.

Every published pick freezes `stat_snapshot` and `engine_version` so the
accuracy record stays auditable and regressions are attributable to a version.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel
from apps.fixtures.models import Fixture


class Market(models.TextChoices):
    MATCH_RESULT = "1x2", "Match Result (1X2)"
    DOUBLE_CHANCE = "dc", "Double Chance"
    OVER_UNDER_25 = "ou_2_5", "Over/Under 2.5"
    BTTS = "btts", "Both Teams To Score"
    CORRECT_SCORE = "cs", "Correct Score"


class Outcome(models.TextChoices):
    PENDING = "pending", "Pending"
    WON = "won", "Won"
    LOST = "lost", "Lost"
    VOID = "void", "Void"


class Tier(models.TextChoices):
    FREE = "free", "Free"
    VIP = "vip", "VIP"


class Prediction(TimeStampedModel):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="predictions")
    market = models.CharField(max_length=16, choices=Market.choices)
    selection = models.CharField(max_length=32, help_text="home | draw | away | over | yes | 2-1")

    # Calibrated probability from the statistical layer, 0-1.
    probability = models.FloatField()
    # Confidence shown to the user, 0-100. Never call this a guarantee — see
    # CLAUDE.md on positioning; the whole product rests on this being honest.
    confidence = models.IntegerField()
    fair_odds = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    market_odds = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    # market_odds / fair_odds - 1. Positive means the book is paying over the
    # model's price. This is the only number that separates a tip from a bet.
    edge = models.FloatField(null=True, blank=True)

    rationale = models.TextField(blank=True, help_text="LLM-written, user-facing")
    # Set when a pick is handed to a batch, cleared if that batch fails, so
    # "needs prose" is one query and a retry can never double-submit.
    rationale_batch = models.ForeignKey(
        "RationaleBatch", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="predictions",
    )
    stat_snapshot = models.JSONField(default=dict, blank=True)
    engine_version = models.CharField(max_length=32, default="0.1.0")

    tier = models.CharField(max_length=8, choices=Tier.choices, default=Tier.FREE, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)

    outcome = models.CharField(
        max_length=8, choices=Outcome.choices, default=Outcome.PENDING, db_index=True
    )
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["fixture__kickoff"]
        constraints = [
            models.UniqueConstraint(
                fields=["fixture", "market", "engine_version"],
                name="uniq_prediction_per_market_version",
            )
        ]
        indexes = [models.Index(fields=["published_at", "tier"])]

    def __str__(self) -> str:
        return f"{self.fixture} — {self.get_market_display()}: {self.selection} ({self.confidence}%)"

    # Raw selections are storage keys ("home_draw"), not something to show a user.
    # Kept on the model so the bot, the API and the web app can't drift apart.
    SELECTION_LABELS = {
        "home": "Home win", "draw": "Draw", "away": "Away win",
        "home_draw": "Home or Draw", "home_away": "Home or Away",
        "draw_away": "Draw or Away",
        "over": "Over 2.5 goals", "under": "Under 2.5 goals",
        "yes": "Yes", "no": "No",
    }

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    @property
    def selection_label(self) -> str:
        return selection_label_for(
            self.market, self.selection,
            self.fixture.home.name, self.fixture.away.name,
        )

    def publish(self, tier: str = Tier.FREE) -> bool:
        """Publish unless the engine isn't confident enough. Returns whether it went out."""
        if self.confidence < settings.MIN_PUBLISH_CONFIDENCE:
            return False
        self.tier = tier
        self.published_at = timezone.now()
        self.save(update_fields=["tier", "published_at", "updated_at"])
        return True


class Slip(TimeStampedModel):
    """
    A curated accumulator — 'Banker of the Day', '2 Odds Daily', 'VIP 5-Fold'.
    This is what actually converts: users buy a slip, not a probability table.
    """

    class Kind(models.TextChoices):
        BANKER = "banker", "Banker of the Day"
        TWO_ODDS = "two_odds", "2 Odds Daily"
        ACCUMULATOR = "acca", "Accumulator"
        VALUE = "value", "Value Picks"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=128)
    for_date = models.DateField(db_index=True)
    tier = models.CharField(max_length=8, choices=Tier.choices, default=Tier.VIP)
    predictions = models.ManyToManyField(Prediction, related_name="slips")

    total_odds = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    outcome = models.CharField(max_length=8, choices=Outcome.choices, default=Outcome.PENDING)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-for_date"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "for_date"], name="uniq_slip_per_kind_per_day")
        ]

    def __str__(self) -> str:
        return f"{self.title} — {self.for_date}"


class AccuracySnapshot(TimeStampedModel):
    """
    Daily rollup powering the public 'our record' page — the thing that makes the
    confidence number credible instead of marketing. Recomputed on settlement.
    """

    for_date = models.DateField(db_index=True)
    market = models.CharField(max_length=16, choices=Market.choices, blank=True)
    tier = models.CharField(max_length=8, choices=Tier.choices, blank=True)

    total = models.IntegerField(default=0)
    won = models.IntegerField(default=0)
    lost = models.IntegerField(default=0)
    void = models.IntegerField(default=0)
    # Return on 1-unit level stakes at market_odds. The honest headline metric:
    # a 70% strike rate at short prices can still lose money.
    roi = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-for_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["for_date", "market", "tier"], name="uniq_accuracy_rollup"
            )
        ]

    @property
    def win_rate(self) -> float:
        decided = self.won + self.lost
        return (self.won / decided * 100) if decided else 0.0


class PredictionView(TimeStampedModel):
    """
    One row per user per prediction unlocked. Doubles as the credit-spend audit
    trail: a user is never charged twice for the same pick.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="views")
    prediction = models.ForeignKey(Prediction, on_delete=models.CASCADE, related_name="views")
    credits_spent = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "prediction"], name="uniq_user_prediction_view")
        ]


class RationaleBatch(TimeStampedModel):
    """
    One submission to the provider's batch endpoint.

    Batch prose is half price and the daily run is the ideal workload for it —
    scheduled, and nobody is waiting on the output. The cost is that results are
    only guaranteed within the completion window, so this row is what lets a
    later poll find its way back to the right picks. It is a record of work in
    flight, not a cache: once the rationales are backfilled onto Predictions, the
    batch is only useful for audit.
    """

    class Status(models.TextChoices):
        # Mirrors the provider's own vocabulary rather than inventing a parallel
        # one — a status here should be greppable against their dashboard.
        VALIDATING = "validating", "Validating"
        IN_PROGRESS = "in_progress", "In progress"
        FINALIZING = "finalizing", "Finalizing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"
        CANCELLING = "cancelling", "Cancelling"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.VALIDATING, Status.IN_PROGRESS, Status.FINALIZING)
    DEAD_STATUSES = (Status.FAILED, Status.EXPIRED, Status.CANCELLED)

    provider_batch_id = models.CharField(max_length=128, unique=True)
    input_file_id = models.CharField(max_length=128, blank=True)
    output_file_id = models.CharField(max_length=128, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.VALIDATING, db_index=True
    )
    requested = models.IntegerField(default=0)
    collected = models.IntegerField(default=0)

    for_date = models.DateField(db_index=True, help_text="Slate this batch was built for")
    collected_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True, help_text="Last poll payload")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.provider_batch_id} ({self.status}, {self.collected}/{self.requested})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    def release_predictions(self) -> int:
        """
        Hand the picks back so a later batch can retry them.

        Called when a batch dies. Without this the FK stays set, the "needs
        prose" query keeps skipping them, and those picks are silently mute
        forever.
        """
        return self.predictions.update(rationale_batch=None)


def selection_label_for(market: str, selection: str, home_name: str, away_name: str) -> str:
    """
    Human-readable form of a raw selection key.

    Team-aware where it helps: "Home win" means little in a list, so 1X2 and
    double chance resolve to actual club names.

    Module-level rather than only a property because the LLM prompt needs the
    same string before a Prediction row exists. A second copy of this mapping is
    how the bot ends up saying "Man City or Draw" while the write-up says
    "home_draw" — which is exactly what happened before this was shared.
    """
    named = {
        "home": home_name, "away": away_name, "draw": "Draw",
        "home_draw": f"{home_name} or Draw",
        "draw_away": f"Draw or {away_name}",
        "home_away": f"{home_name} or {away_name}",
    }
    if market in (Market.MATCH_RESULT, Market.DOUBLE_CHANCE):
        return named.get(selection, selection)
    return Prediction.SELECTION_LABELS.get(selection, selection.upper())
