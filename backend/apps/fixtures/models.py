"""
Fixture data ingested from API-Football (v3).

Every row keeps `api_id` so syncs are idempotent upserts, and `raw` so we can
re-derive features later without re-paying for the API call. The stat pack the
engine reasons over is frozen onto the Prediction itself (see predictions app),
so a published pick stays auditable even after these rows move on.
"""

from django.db import models

from apps.common.models import TimeStampedModel


class League(TimeStampedModel):
    api_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=128)
    country = models.CharField(max_length=64, blank=True)
    logo_url = models.URLField(blank=True)
    season = models.IntegerField(help_text="Current season year, e.g. 2026")
    # Coverage is deliberately narrow — depth over breadth beats a thin global feed.
    is_active = models.BooleanField(default=True, db_index=True)
    # The provider's own payload, kept like every other ingested row. Carries the
    # per-season `coverage` flags, which say whether odds and injuries exist for
    # this league on this plan — worth having without paying for the call again.
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["country", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.country})"


class Team(TimeStampedModel):
    api_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=128)
    short_name = models.CharField(max_length=32, blank=True)
    logo_url = models.URLField(blank=True)
    country = models.CharField(max_length=64, blank=True)

    # Rolling strength ratings maintained by the statistical layer.
    elo = models.FloatField(default=1500.0)

    # Overall (venue-agnostic) ratios against the league average. Still the
    # honest summary of a side, and the target the venue splits are shrunk
    # toward when a team has played too few games at one venue to trust.
    attack_strength = models.FloatField(default=1.0)
    defence_strength = models.FloatField(default=1.0)

    # Venue splits. A side that is a fortress at home and dreadful away averages
    # into mush under a single rating, which is exactly the fixture the model
    # most needs to get right. Normalised against *venue-specific* league
    # averages, so each is centred on 1.0 and home advantage is measured per
    # league rather than assumed — see ratings.league_venue_averages.
    home_attack_strength = models.FloatField(default=1.0)
    home_defence_strength = models.FloatField(default=1.0)
    away_attack_strength = models.FloatField(default=1.0)
    away_defence_strength = models.FloatField(default=1.0)

    ratings_updated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class Fixture(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        LIVE = "live", "Live"
        FINISHED = "finished", "Finished"
        POSTPONED = "postponed", "Postponed"
        CANCELLED = "cancelled", "Cancelled"

    api_id = models.BigIntegerField(unique=True)
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="fixtures")
    home = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="home_fixtures")
    away = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="away_fixtures")

    kickoff = models.DateTimeField(db_index=True)
    round = models.CharField(max_length=64, blank=True)
    venue = models.CharField(max_length=128, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )

    home_goals = models.IntegerField(null=True, blank=True)
    away_goals = models.IntegerField(null=True, blank=True)
    home_goals_ht = models.IntegerField(null=True, blank=True)
    away_goals_ht = models.IntegerField(null=True, blank=True)

    raw = models.JSONField(default=dict, blank=True)

    # Elo is path-dependent and applied incrementally, so a settled fixture must
    # contribute exactly once no matter how often settlement re-runs.
    elo_applied = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["kickoff"]
        indexes = [models.Index(fields=["kickoff", "status"])]

    def __str__(self) -> str:
        return f"{self.home} vs {self.away} @ {self.kickoff:%Y-%m-%d %H:%M}"

    @property
    def is_settled(self) -> bool:
        return self.status == self.Status.FINISHED and self.home_goals is not None


class Odds(TimeStampedModel):
    """
    Bookmaker prices. Used two ways: to show the user what the pick is worth, and
    as a sanity check on the model — a pick whose implied probability is wildly
    below the book's is usually the model being wrong, not the market.
    """

    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="odds")
    bookmaker = models.CharField(max_length=64)
    market = models.CharField(max_length=32, help_text="1x2 | ou_2_5 | btts | dc")
    selection = models.CharField(max_length=32, help_text="home | draw | away | over | ...")
    price = models.DecimalField(max_digits=8, decimal_places=3)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["fixture", "market"])]
        constraints = [
            models.UniqueConstraint(
                fields=["fixture", "bookmaker", "market", "selection"],
                name="uniq_odds_per_book_selection",
            )
        ]


class Injury(TimeStampedModel):
    """Availability is the single highest-signal non-statistical input."""

    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="injuries")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="injuries")
    player_name = models.CharField(max_length=128)
    reason = models.CharField(max_length=128, blank=True)
    type = models.CharField(max_length=32, blank=True, help_text="missing | questionable")
