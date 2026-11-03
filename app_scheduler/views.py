# app_scheduler/views.py

from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View

from app_helper.access import admin_required_json

from app_scheduler.management.commands.run_scheduler import (
    FIXED_DAILY_TIMES,
    SCHEDULE,
    next_fixed_daily_run,
)
from app_scheduler.progress import get_task_progress

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)


def _nk_sync_state() -> dict:
    """Текущее состояние синхронизации Национального каталога."""
    from app_cz.services.nk_sync_state import get_sync_state
    return get_sync_state()


@method_decorator(admin_required_json, name='dispatch')
class SchedulerStatusView(View):
    """
    API для получения расписания периодических задач.
    GET /scheduler/status/
    """

    def get(self, request):
        from django_tasks_db.models import DBTaskResult
        from django_tasks.base import TaskResultStatus

        from app_scheduler.registry import get_scheduled_tasks

        task_map = get_scheduled_tasks()

        now = timezone.now()
        schedule_data = []

        # Пути задач: статусы и последние запуски грузим пачкой, а не по
        # 4 запроса на каждую задачу (эндпоинт опрашивается каждые 5 секунд).
        paths = []
        path_by_name = {}
        for name, interval, description in SCHEDULE:
            task_func = task_map.get(name)
            if not task_func:
                continue
            original_func = task_func.func
            task_path = f'{original_func.__module__}.{original_func.__name__}'
            paths.append(task_path)
            path_by_name[name] = task_path

        last_runs = self._last_results(
            DBTaskResult,
            paths,
            [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED],
            '-finished_at',
            ('task_path', 'status', 'finished_at'),
        )
        last_failures = self._last_results(
            DBTaskResult,
            paths,
            [TaskResultStatus.FAILED],
            '-finished_at',
            ('task_path', 'status', 'finished_at'),
        )
        running_tasks = self._last_results(
            DBTaskResult,
            paths,
            [TaskResultStatus.RUNNING],
            '-started_at',
            ('task_path', 'started_at'),
        )
        queued_paths = set(
            DBTaskResult.objects
            .filter(task_path__in=paths, status=TaskResultStatus.READY)
            .values_list('task_path', flat=True)
            .distinct()
        )

        for name, interval, description in SCHEDULE:
            task_path = path_by_name.get(name)
            if not task_path:
                continue

            last_run = last_runs.get(task_path)
            last_error = last_failures.get(task_path)
            running = running_tasks.get(task_path)
            is_queued = task_path in queued_paths

            if last_run and last_run['finished_at']:
                last_run_str = last_run['finished_at'].strftime('%d.%m.%Y %H:%M:%S')
                last_status = last_run['status']
            else:
                last_run_str = None
                last_status = None

            # Задачи с фиксированным временем суток (например, 00:30).
            if name in FIXED_DAILY_TIMES:
                hour, minute = FIXED_DAILY_TIMES[name]
                target = next_fixed_daily_run(name, last_run, now)
                interval_display = f'ежедневно {hour:02d}:{minute:02d}'
                next_run_str = (
                    'Сейчас (при следующей проверке)'
                    if target <= now
                    else target.strftime('%d.%m.%Y %H:%M:%S')
                )
            elif last_run and last_run['finished_at']:
                next_run = last_run['finished_at'] + interval
                interval_display = self._format_interval(int(interval.total_seconds()))
                next_run_str = (
                    'Сейчас (при следующей проверке)'
                    if next_run <= now
                    else next_run.strftime('%d.%m.%Y %H:%M:%S')
                )
            else:
                interval_display = self._format_interval(int(interval.total_seconds()))
                next_run_str = 'Первый запуск'

            schedule_data.append({
                'name': name,
                'description': description,
                'interval_seconds': int(interval.total_seconds()),
                'interval_display': interval_display,
                'last_run': last_run_str,
                'last_status': last_status,
                'next_run': next_run_str,
                'has_recent_error': bool(
                    last_error and
                    last_error['finished_at'] and
                    (now - last_error['finished_at']) < interval
                ),
                'is_running': bool(running),
                'is_queued': is_queued,
                'started_at': (
                    running['started_at'].strftime('%d.%m.%Y %H:%M:%S')
                    if running and running['started_at'] else None
                ),
                'progress': get_task_progress(name) if running else None,
            })

        return JsonResponse({
            'is_error': False,
            'current_time': now.strftime('%d.%m.%Y %H:%M:%S'),
            'scheduler_check_interval': 60,
            'schedule': schedule_data,
            # Текущее состояние синхронизации НК (видно и для ручного запуска,
            # и для фоновой задачи — окно «Фоновые задачи» отражает конфликты).
            'nk_sync': _nk_sync_state(),
            # Состояние системы: последняя проверка по каждому сервису.
            'system_health': self._system_health_state(),
        })

    @staticmethod
    def _last_results(model, paths, statuses, order_field, fields):
        """Последняя запись по каждому task_path одним запросом."""
        if not paths:
            return {}
        rows = (
            model.objects
            .filter(task_path__in=paths, status__in=statuses)
            .order_by('task_path', order_field)
            .values(*fields)
            .distinct('task_path')
        )
        return {row['task_path']: row for row in rows}

    def _system_health_state(self) -> dict:
        """Последняя проверка по каждому сервису для виджета состояния."""
        from app_event.models import HealthCheck

        checks = (
            HealthCheck.objects
            .order_by('service', '-checked_at')
            .distinct('service')
        )
        seen = {
            check.service: {
                'service': check.service,
                'name': check.name,
                'is_ok': check.is_ok,
                'level': check.level,
                'message': check.message,
                'checked_at': check.checked_at.strftime('%d.%m.%Y %H:%M:%S'),
            }
            for check in checks
        }
        return {'is_ok': all(c['is_ok'] for c in seen.values()), 'services': list(seen.values())}

    def _format_interval(self, seconds: int) -> str:
        if seconds < 60:
            return f'{seconds} сек'
        if seconds < 3600:
            return f'{seconds // 60} мин'
        if seconds < 86400:
            return f'{seconds // 3600} ч'
        days = seconds // 86400
        return f'{days} дн'


@method_decorator(admin_required_json, name='dispatch')
class SchedulerRunView(View):
    """
    Ручной запуск периодической задачи (только администратор).
    POST /scheduler/run/<name>/ — ставит задачу в очередь воркера.
    """

    def post(self, request, name):
        from django_tasks.base import TaskResultStatus
        from django_tasks_db.models import DBTaskResult

        from app_scheduler.registry import get_scheduled_tasks

        task_func = get_scheduled_tasks().get(name)
        if task_func is None:
            return JsonResponse(
                {'is_error': True, 'message': f'Задача «{name}» не найдена.'},
                status=404,
            )

        # Не дублируем задачу, если она уже в очереди или выполняется.
        original = task_func.func
        task_path = f'{original.__module__}.{original.__name__}'
        pending = DBTaskResult.objects.filter(
            task_path=task_path,
            status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING],
        ).exists()
        if pending:
            return JsonResponse(
                {'is_error': True, 'message': 'Задача уже в очереди или выполняется.'},
                status=409,
            )

        try:
            result = task_func.enqueue()
        except Exception as exc:  # noqa: BLE001 — сообщаем причину пользователю
            return JsonResponse(
                {'is_error': True, 'message': f'Не удалось запустить задачу: {exc}'},
                status=500,
            )

        return JsonResponse({
            'is_error': False,
            'message': 'Задача поставлена в очередь.',
            'task_id': str(getattr(result, 'id', '') or ''),
        })
