from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("plans/", views.plans, name="plans"),
    path("checkout/", views.checkout, name="checkout"),
    path("credits/", views.credit_history, name="credit-history"),
]
