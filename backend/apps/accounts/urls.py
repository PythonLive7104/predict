from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = "accounts"

urlpatterns = [
    path("auth/telegram/", views.telegram_login, name="telegram-login"),
    path("auth/link/", views.link_start, name="link-start"),
    path("auth/link/<str:code>/", views.link_status, name="link-status"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", views.me, name="me"),
]
