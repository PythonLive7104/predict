from django.urls import path

from . import views

app_name = "predictions"

urlpatterns = [
    path("picks/today/", views.todays_picks, name="todays-picks"),
    path("picks/<int:pk>/unlock/", views.unlock_pick, name="unlock-pick"),
    path("slips/today/", views.todays_slips, name="todays-slips"),
    path("record/", views.record, name="record"),
]
