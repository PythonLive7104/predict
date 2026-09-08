from django.contrib import admin

from .models import AccuracySnapshot, Prediction, PredictionView, Slip


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_display = ("fixture", "market", "selection", "confidence", "tier", "outcome", "published_at")
    list_filter = ("market", "tier", "outcome", "engine_version")
    search_fields = ("fixture__home__name", "fixture__away__name")
    readonly_fields = ("stat_snapshot",)
    date_hierarchy = "created_at"


@admin.register(Slip)
class SlipAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "for_date", "tier", "total_odds", "outcome")
    list_filter = ("kind", "tier", "outcome")
    filter_horizontal = ("predictions",)


@admin.register(AccuracySnapshot)
class AccuracySnapshotAdmin(admin.ModelAdmin):
    list_display = ("for_date", "market", "tier", "total", "won", "lost", "roi")
    list_filter = ("market", "tier")


admin.site.register(PredictionView)
