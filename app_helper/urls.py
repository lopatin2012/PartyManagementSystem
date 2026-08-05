# app_helper/urls.py

from django.urls import path

from app_helper import views


urlpatterns = [
    path('api/v1/status_service/', view=views.api_status_service, name='api-v1-status_service'),
]
