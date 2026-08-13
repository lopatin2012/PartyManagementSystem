# app_scheduler/urls.py

from django.urls import path
from app_scheduler.views import SchedulerStatusView

urlpatterns = [
    path('status/', SchedulerStatusView.as_view(), name='status'),
]
