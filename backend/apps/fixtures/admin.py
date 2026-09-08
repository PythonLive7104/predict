from django.contrib import admin

from .models import Fixture, Injury, League, Odds, Team


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "season", "api_id", "is_active")
    list_editable = ("is_active",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "elo", "attack_strength", "defence_strength", "ratings_updated_at")
    search_fields = ("name",)


@admin.register(Fixture)
class FixtureAdmin(admin.ModelAdmin):
    list_display = ("__str__", "league", "status", "home_goals", "away_goals")
    list_filter = ("status", "league")
    date_hierarchy = "kickoff"
    readonly_fields = ("raw",)


admin.site.register([Odds, Injury])
