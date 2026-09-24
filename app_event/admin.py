from django.contrib import admin

from app_event.models import EventLog, HealthCheck, NotificationRecipient


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
    list_display = ('group', 'email', 'is_active', 'created_at')
    list_filter = ('group', 'is_active')
    search_fields = ('email', 'group')
    ordering = ('group', 'email')
