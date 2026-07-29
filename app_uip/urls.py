# app_uip/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from app_uip import views

router = DefaultRouter()
router.register('v1/status_parties', views.UIPStatusViewSet, basename='api-v1-status-parties')

urlpatterns = [
    path('', include(router.urls)),
]
