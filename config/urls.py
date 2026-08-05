# config/urls.py

from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from config.settings import DEBUG

urlpatterns = [
    path('auth/', include('django.contrib.auth.urls')), # Стандартная аутентификация.
    path('admin/', admin.site.urls, name='admin'),
    path('', include('app_page.urls')), # Страницы.
    path('wms/', include('app_wms.urls')), # Склад.
    path('factory/', include('app_factory.urls')), # Модуль производства.
    path('cz/', include('app_cz.urls')), # Взаимодействие с Честным Знаком.
    path('uip/', include('app_uip.urls')), # Взаимодействие с УИП.
    path('helper/', include('app_helper.urls')), # Помощник сервиса.

    # Документация API.
    # Ссылка на схему (JSON/YAML файл).
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

    # Ссылка на интерактивный интерфейс Swagger UI.
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]

if DEBUG:
    urlpatterns += staticfiles_urlpatterns()
