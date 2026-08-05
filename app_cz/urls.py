# app_cz/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from app_cz import views

router = DefaultRouter()
router.register('v1/codes', views.CISCodeViewSet, basename='api-v1-codes')
router.register('v1/reserved_parties', views.ReservedPartyViewSet, basename='api-v1-reserved-parties')
router.register('v1/parties', views.ProductionPartyViewSet, basename='api-v1-parties')

urlpatterns = [
    # API endpoints.
    path('api/', include(router.urls)),

    path('api/get-suz-certificates/', view=views.api_get_suz_certificates, name='api-get-suz-certificates'),
    path('api/setup-suz-account/', view=views.api_setup_suz_account, name='api-setup-suz-account'),
    path('api/reset-suz-account/', view=views.api_reset_suz_account, name='api-reset-suz-account'),
    path('api/get-auth-key/', view=views.api_get_auth_key, name='api-get-auth-key'),
    path('api/refresh-suz-token/', view=views.api_refresh_suz_token, name='api-refresh-suz-token'),
    # УИП.
    # Генерация и резервирование номера партии на стороне ЧЗ.
    path('api/generate-parties/', view=views.api_generate_parties, name='api-generate-parties'),
    # Резервирование своего номера партии.
    path('api/reserve-parties/', view=views.api_reserve_parties, name='api-reserve-parties'),
    # Получение всех зарезервированных партий.
    path(
        'api/get-all-reserved-parties/',
        view=views.api_get_all_reserved_parties,
        name='get-all-reserved-parties'
    ),
    # Снятие с резерва партии.
    path(
        'api/close-party-reservation/',
        view=views.api_close_party_reservation,
        name='close-party-reservation'
    ),

    # Синхронизация кодов задания.
    path(
        'api/codes/sync-task/',
        view=views.api_sync_codes_task,
        name='api-sync-codes-task'
    ),

    # Для общения с другими модулями.
    path('api/v1/generate-uip/', view=views.api_generate_uip, name='generate-uip'),
]
