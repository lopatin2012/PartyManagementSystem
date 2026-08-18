from django.contrib import admin

from app_event.models import EventLog


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'module', 'level', 'message', 'actor')
    list_filter = ('module', 'level')
    search_fields = ('message', 'actor')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)
