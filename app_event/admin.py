from django.contrib import admin

from app_event.models import (
    EventLog, HealthCheck, NotificationRecipient, RequestMetric,
)


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'module', 'level', 'message', 'actor')
    list_filter = ('module', 'level')
    search_fields = ('message', 'actor')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)


@admin.register(HealthCheck)
class HealthCheckAdmin(admin.ModelAdmin):
    list_display = ('checked_at', 'service', 'name', 'is_ok', 'level', 'message')
    list_filter = ('service', 'level', 'is_ok')
    search_fields = ('name', 'message', 'service')
    readonly_fields = ('checked_at',)
    ordering = ('-checked_at',)
    date_hierarchy = 'checked_at'


@admin.register(NotificationRecipient)
class NotificationRecipientAdmin(admin.ModelAdmin):
    list_display = ('group', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('group__name',)
    ordering = ('group__name',)


@admin.register(RequestMetric)
class RequestMetricAdmin(admin.ModelAdmin):
    """Минутные агрегаты запросов — только просмотр (основной UI: /monitoring/)."""

    list_display = (
        'bucket_start', 'group', 'method', 'status_class', 'traffic_class',
        'count', 'errors', 'slow_count', 'total_ms', 'max_ms',
        'db_ms', 'db_queries', 'peak_inflight',
    )
    ordering = ('-bucket_start',)
    date_hierarchy = 'bucket_start'
    show_full_result_count = False
    list_per_page = 50

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
