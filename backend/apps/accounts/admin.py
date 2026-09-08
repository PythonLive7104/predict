from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("__str__", "telegram_id", "source", "referral_code", "is_blocked", "last_seen_at")
    list_filter = ("source", "is_blocked", "is_staff")
    search_fields = ("telegram_username", "telegram_id", "username", "email")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Telegram", {"fields": ("telegram_id", "telegram_username", "language_code", "source")}),
        ("Growth", {"fields": ("referral_code", "referred_by", "is_blocked", "last_seen_at")}),
    )
