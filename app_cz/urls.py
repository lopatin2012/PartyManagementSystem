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

    path('api/get-suz-certificates/', view=views.api_get_suz_certificates, name='api-get-suz-certificates'),
    path('api/setup-suz-account/', view=views.api_setup_suz_account, name='api-setup-suz-account'),
    path('api/reset-suz-account/', view=views.api_reset_suz_account, name='api-reset-suz-account'),
    path('api/get-auth-key/', view=views.api_get_auth_key, name='api-get-auth-key')
]
