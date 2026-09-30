# app_scheduler/views.py

from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View

from app_helper.access import admin_required_json

from app_scheduler.management.commands.run_scheduler import SCHEDULE

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

        for name, interval, description in SCHEDULE:
            task_func = task_map.get(name)
            if not task_func:
                continue

            original_func = task_func.func
            task_path = f'{original_func.__module__}.{original_func.__name__}'

            # Последний запуск
            last_run = (
                DBTaskResult.objects
                .filter(task_path=task_path)
                .filter(status__in=[TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED])
                .order_by('-finished_at')
                .first()
            )

            # Последняя ошибка (если была)
            last_error = (
                DBTaskResult.objects
                .filter(task_path=task_path, status=TaskResultStatus.FAILED)
                .order_by('-finished_at')
                .first()
            )

            if last_run and last_run.finished_at:
                next_run = last_run.finished_at + interval
                if next_run <= now:
                    next_run_str = 'Сейчас (при следующей проверке)'
                else:
                    next_run_str = next_run.strftime('%d.%m.%Y %H:%M:%S')
                last_run_str = last_run.finished_at.strftime('%d.%m.%Y %H:%M:%S')
                last_status = last_run.status
            else:
                next_run_str = 'Первый запуск'
                last_run_str = None
                last_status = None

            schedule_data.append({
                'name': name,
                'description': description,
                'interval_seconds': int(interval.total_seconds()),
                'interval_display': self._format_interval(int(interval.total_seconds())),
                'last_run': last_run_str,
                'last_status': last_status,
                'next_run': next_run_str,
                'has_recent_error': bool(
                    last_error and
                    last_error.finished_at and
                    (now - last_error.finished_at) < interval
                ),
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

    def _system_health_state(self) -> dict:
        """Последняя проверка по каждому сервису для виджета состояния."""
        from app_event.models import HealthCheck

        seen = {}
        for check in HealthCheck.objects.order_by('-checked_at'):
            if check.service in seen:
                continue
            seen[check.service] = {
                'service': check.service,
                'name': check.name,
                'is_ok': check.is_ok,
                'level': check.level,
                'message': check.message,
                'checked_at': check.checked_at.strftime('%d.%m.%Y %H:%M:%S'),
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
