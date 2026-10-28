# app_event/urls.py

from django.urls import path

from app_event import views

urlpatterns = [
    path('', views.MonitoringDashboardView.as_view(), name='monitoring'),
    path(
        'api/metrics/',
        views.MonitoringMetricsView.as_view(),
        name='monitoring_metrics',
    ),
]
