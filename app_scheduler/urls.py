# app_scheduler/urls.py

from django.urls import path
from app_scheduler.views import SchedulerRunView, SchedulerStatusView

urlpatterns = [
    path('status/', SchedulerStatusView.as_view(), name='status'),
    path('run/<str:name>/', SchedulerRunView.as_view(), name='run'),
]
