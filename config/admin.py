# config/admin.py

from django.contrib import admin

from config.models import ExternalService


@admin.register(ExternalService)
class ExternalServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'service_type', 'ip_address', 'port_address', 'is_active')
    list_filter = ('service_type', 'is_active')
    search_fields = ('name', 'ip_address')
    ordering = ('name',)

    fieldsets = (
        ('Основные данные', {
            'fields': ('name', 'service_type', 'photo', 'is_active')
        }),
        ('Подключение', {
            'fields': ('ip_address', 'port_address')
        }),
    )
