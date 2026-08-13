# app_scheduler/admin.py

import json
from datetime import datetime, timedelta

from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils import timezone

from django_tasks_db.models import DBTaskResult
from django_tasks.base import TaskResultStatus

# === СНАЧАЛА снимаем стандартную регистрацию пакета для устранения ошибки уже смонтированной таблицы ===
try:
    admin.site.unregister(DBTaskResult)
except admin.sites.NotRegistered:
    pass


class DBTaskResultAdmin(admin.ModelAdmin):
    """Админка для просмотра и управления фоновыми задачами django-tasks."""

    list_display = (
        'short_id',
        'task_name_display',
        'status_colored',
        'queue_name',
        'priority',
        'enqueued_at_short',
        'started_at_short',
        'finished_at_short',
        'duration_display',
    )

    list_filter = (
        'status',
        'queue_name',
        'backend_name',
    )

    search_fields = (
        'id',
        'task_path',
    )

    readonly_fields = (
        'id',
        'task_name',
        'status',
        'enqueued_at',
        'started_at',
        'finished_at',
        'args_kwargs_pretty',
        'return_value_pretty',
        'exception_class_path',
        'traceback_pretty',
        'queue_name',
        'backend_name',
        'priority',
        'run_after',
        'worker_ids',
        'task_path',
    )

    fieldsets = (
        ('Основная информация', {
            'fields': (
                'id',
                'task_name',
                'task_path',
                'status',
                'queue_name',
                'backend_name',
                'priority',
                'run_after',
            )
        }),
        ('Временные метки', {
            'fields': (
                'enqueued_at',
                'started_at',
                'finished_at',
            )
        }),
        ('Параметры и результат', {
            'fields': (
                'args_kwargs_pretty',
                'return_value_pretty',
                'worker_ids',
            )
        }),
        ('Ошибка (если есть)', {
            'fields': (
                'exception_class_path',
                'traceback_pretty',
            ),
            'classes': ('collapse',),
        }),
    )

    ordering = ('-enqueued_at',)
    list_per_page = 25

    actions = ['restart_failed_tasks', 'delete_successful_tasks', 'delete_old_tasks']

    # === Запрет ручного создания/изменения ===
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Разрешаем только просмотр
        return False

    # === Отображение в списке ===
    @admin.display(description='ID', ordering='id')
    def short_id(self, obj):
        """Короткий UUID для компактного отображения."""
        return str(obj.id)[:8]

    @admin.display(description='Задача', ordering='task_path')
    def task_name_display(self, obj):
        """Имя задачи через property модели."""
        return obj.task_name

    @admin.display(description='Статус', ordering='status')
    def status_colored(self, obj):
        """Цветная плашка статуса."""
        colors = {
            TaskResultStatus.READY: '#FABB1C',  # жёлтый
            TaskResultStatus.RUNNING: '#6495ED',  # синий
            TaskResultStatus.SUCCESSFUL: '#BBCF32',  # зелёный
            TaskResultStatus.FAILED: '#ff6b7a',  # красный
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 3px 10px; border-radius: 4px; font-size: 0.85rem; '
            'font-weight: 600;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description='Поставлена', ordering='enqueued_at')
    def enqueued_at_short(self, obj):
        if not obj.enqueued_at:
            return '—'
        return obj.enqueued_at.strftime('%d.%m %H:%M:%S')

    @admin.display(description='Начата', ordering='started_at')
    def started_at_short(self, obj):
        if not obj.started_at:
            return '—'
        return obj.started_at.strftime('%d.%m %H:%M:%S')

    @admin.display(description='Завершена', ordering='finished_at')
    def finished_at_short(self, obj):
        if not obj.finished_at:
            return '—'
        return obj.finished_at.strftime('%d.%m %H:%M:%S')

    @admin.display(description='Длительность')
    def duration_display(self, obj):
        """Вычисляем время выполнения."""
        if not obj.started_at or not obj.finished_at:
            return '—'
        delta = obj.finished_at - obj.started_at
        total_seconds = delta.total_seconds()
        if total_seconds < 1:
            return f'{total_seconds * 1000:.0f} мс'
        if total_seconds < 60:
            return f'{total_seconds:.1f} сек'
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)
        return f'{minutes} мин {seconds} сек'

    # === Pretty-отображение JSON-полей ===
    @admin.display(description='Аргументы (args/kwargs)')
    def args_kwargs_pretty(self, obj):
        if not obj.args_kwargs:
            return '—'
        try:
            pretty = json.dumps(obj.args_kwargs, indent=2, ensure_ascii=False)
            return format_html(
                '<pre style="background: rgba(0,0,0,0.2); padding: 8px; '
                'border-radius: 4px; font-size: 0.85rem; max-height: 200px; '
                'overflow: auto;">{}</pre>',
                pretty,
            )
        except (TypeError, ValueError):
            return str(obj.args_kwargs)

    @admin.display(description='Возвращённое значение')
    def return_value_pretty(self, obj):
        if obj.return_value is None:
            return '—'
        try:
            pretty = json.dumps(obj.return_value, indent=2, ensure_ascii=False)
            return format_html(
                '<pre style="background: rgba(187, 207, 50, 0.1); padding: 8px; '
                'border-radius: 4px; font-size: 0.85rem; max-height: 200px; '
                'overflow: auto; color: #BBCF32;">{}</pre>',
                pretty,
            )
        except (TypeError, ValueError):
            return str(obj.return_value)

    @admin.display(description='Traceback')
    def traceback_pretty(self, obj):
        if not obj.traceback:
            return '—'
        return format_html(
            '<pre style="background: rgba(220, 53, 69, 0.1); color: #ff6b7a; '
            'padding: 10px; border-radius: 4px; font-size: 0.82rem; '
            'max-height: 400px; overflow: auto; white-space: pre-wrap;">{}</pre>',
            obj.traceback,
        )

    @admin.action(description='⟳ Перезапустить выбранные FAILED задачи')
    def restart_failed_tasks(self, request, queryset):
        """Создаёт новые задачи на основе упавших."""
        restarted = 0
        for db_result in queryset.filter(status=TaskResultStatus.FAILED):
            try:
                task = db_result.task
                task.enqueue(*db_result.args_kwargs['args'], **db_result.args_kwargs['kwargs'])
                restarted += 1
            except Exception as e:
                self.message_user(
                    request,
                    f'Не удалось перезапустить задачу {db_result.id}: {e}',
                    level='error',
                )
        self.message_user(
            request,
            f'Перезапущено задач: {restarted}',
            level='success',
        )

    @admin.action(description='🗑 Удалить выполненные (SUCCESSFUL) задачи')
    def delete_successful_tasks(self, request, queryset):
        count, _ = queryset.filter(status=TaskResultStatus.SUCCESSFUL).delete()
        self.message_user(request, f'Удалено задач: {count}', level='success')

    @admin.action(description='🗑 Удалить задачи старше 7 дней')
    def delete_old_tasks(self, request, queryset):
        threshold = timezone.now() - timedelta(days=7)
        count, _ = queryset.filter(enqueued_at__lt=threshold).delete()
        self.message_user(request, f'Удалено старых задач: {count}', level='success')


# === Регистрируем админку ===
admin.site.register(DBTaskResult, DBTaskResultAdmin)
