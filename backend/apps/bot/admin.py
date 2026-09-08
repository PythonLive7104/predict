from django.contrib import admin

from .models import BotSession, Broadcast, Delivery


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "scheduled_for", "sent_count", "failed_count")
    list_filter = ("status",)
    actions = ["send_now"]

    @admin.action(description="Send selected broadcasts now")
    def send_now(self, request, queryset):
        from .tasks import send_broadcast

        for broadcast in queryset:
            send_broadcast.delay(broadcast.pk)
        self.message_user(request, f"Queued {queryset.count()} broadcast(s)")


admin.site.register([BotSession, Delivery])
