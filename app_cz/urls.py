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
    path(
        'api/reserve-draft-uip/',
        views.api_reserve_draft_uip,
        name='api-reserve-draft-uip'
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

    # Синхронизация с внешним сервисом (Молвест.Маркировка).
    # Приёмник задания (внешний сервис «пушит» сюда свои задания).
    path(
        'api/tasks/receive/',
        view=views.api_receive_external_task,
        name='api-receive-external-task'
    ),
    # Ручная синхронизация кодов задания.
    path(
        'api/tasks/sync-codes/',
        view=views.api_sync_task_codes,
        name='api-sync-task-codes'
    ),

    # Для общения с другими модулями.
    path('api/v1/generate-uip/', view=views.api_generate_uip, name='generate-uip'),

    # ==========================================
    # Интерфейсные API-методы
    # ==========================================
    # Синхронизация УИП из Честного Знака и генерация УИП.
    path('uip/sync/', views.SyncPartiesView.as_view(), name='uip_sync'),
    path('uip/generate/', views.GenerateUIPView.as_view(), name='uip_generate'),

    # Синхронизация заданий с внешним сервисом (Молвест.Маркировка).
    path('sync/', view=views.SyncTasksView.as_view(), name='sync_tasks'),
    path('sync/task-codes/', view=views.SyncTaskCodesView.as_view(), name='sync_task_codes'),
    path('sync/all/', view=views.SyncAllTasksView.as_view(), name='sync_all_tasks'),

    # Национальный каталог.
    path('nk/', view=views.NationalCatalogView.as_view(), name='national_catalog'),
    path('nk/api/sync/', view=views.NKSyncProductsView.as_view(), name='nk_sync_products'),
    path('nk/api/progress/', view=views.NKSyncProgressView.as_view(), name='nk_sync_progress'),
    path('nk/api/product/create/', view=views.NKProductCreateView.as_view(), name='nk_product_create'),
    path('nk/api/product/', view=views.NKProductDetailView.as_view(), name='nk_product_detail'),
]
