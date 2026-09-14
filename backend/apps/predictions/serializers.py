"""
Serializers with a deliberate gate: a locked pick must never carry its selection.

`PredictionSerializer` takes an `unlocked` flag from the view. When false the
selection, rationale and odds are stripped server-side — not hidden in CSS, which
is how paywalled tip sites leak their whole product to anyone who opens devtools.
"""

from rest_framework import serializers

from .models import AccuracySnapshot, Prediction, Slip


class FixtureBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    home = serializers.CharField(source="home.name")
    away = serializers.CharField(source="away.name")
    home_logo = serializers.CharField(source="home.logo_url")
    away_logo = serializers.CharField(source="away.logo_url")
    league = serializers.CharField(source="league.name")
    kickoff = serializers.DateTimeField()
    status = serializers.CharField()
    home_goals = serializers.IntegerField(allow_null=True)
    away_goals = serializers.IntegerField(allow_null=True)


class PredictionSerializer(serializers.ModelSerializer):
    fixture = FixtureBriefSerializer(read_only=True)
    # Everything about the pick itself is withheld until it is unlocked — the
    # market and the confidence included. A locked row carries the fixture and
    # nothing else, matching the bot exactly; the paywall has to hold at the API
    # or hiding it in the UI is decoration anyone can curl straight past.
    market = serializers.SerializerMethodField()
    market_label = serializers.SerializerMethodField()
    confidence = serializers.SerializerMethodField()
    selection = serializers.SerializerMethodField()
    selection_label = serializers.SerializerMethodField()
    rationale = serializers.SerializerMethodField()
    market_odds = serializers.SerializerMethodField()
    edge = serializers.SerializerMethodField()
    unlocked = serializers.SerializerMethodField()

    class Meta:
        model = Prediction
        fields = (
            "id", "fixture", "market", "market_label", "selection", "selection_label",
            "confidence", "market_odds", "edge", "rationale", "tier", "outcome",
            "unlocked", "published_at",
        )

    @property
    def _unlocked(self) -> bool:
        return bool(self.context.get("unlocked"))

    def get_unlocked(self, obj) -> bool:
        return self._unlocked

    def get_selection(self, obj):
        return obj.selection if self._unlocked else None

    def get_selection_label(self, obj):
        return obj.selection_label if self._unlocked else None

    def get_rationale(self, obj):
        return obj.rationale if self._unlocked else ""

    def get_market_odds(self, obj):
        return obj.market_odds if self._unlocked else None

    def get_market(self, obj):
        return obj.market if self._unlocked else None

    def get_market_label(self, obj):
        return obj.get_market_display() if self._unlocked else None

    def get_confidence(self, obj):
        return obj.confidence if self._unlocked else None

    def get_edge(self, obj):
        # Edge is derived from the price, so publishing it on a locked row
        # leaks most of what withholding market_odds was protecting.
        return obj.edge if self._unlocked else None


class SlipSerializer(serializers.ModelSerializer):
    predictions = serializers.SerializerMethodField()
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Slip
        fields = (
            "id", "kind", "kind_label", "title", "for_date", "tier",
            "total_odds", "outcome", "predictions",
        )

    def get_predictions(self, obj):
        return PredictionSerializer(
            obj.predictions.all(), many=True, context=self.context
        ).data


class AccuracySnapshotSerializer(serializers.ModelSerializer):
    win_rate = serializers.FloatField(read_only=True)

    class Meta:
        model = AccuracySnapshot
        fields = ("for_date", "market", "tier", "total", "won", "lost", "void", "roi", "win_rate")
