# app_cz/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from app_cz import views

router = DefaultRouter()
router.register('codes', views.CISCodeViewSet, basename='api-codes')
router.register('uips', views.UIViewSet, basename='api-uips')
router.register('parties', views.ProductionPartyViewSet, basename='api-parties')

urlpatterns = [
    # API endpoints.
    path('api/', include(router.urls)),
]
