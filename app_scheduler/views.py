# app_scheduler/views.py

from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.views import View

from app_scheduler.management.commands.run_scheduler import SCHEDULE

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)


@method_decorator(staff_member_required, name='dispatch')
class SchedulerStatusView(View):
    """
    API для получения расписания периодических задач.
    GET /scheduler/status/
    """

    def get(self, request):
        from django_tasks_db.models import DBTaskResult
        from django_tasks.base import TaskResultStatus
        from app_scheduler.tasks import (
            refresh_suz_token_task,
            cleanup_expired_reserved_uips_task,
            close_unused_registered_uips_task,
            archive_stale_closed_uips_task,
            cleanup_old_logs_task,
            sync_external_parties_codes_task,
            sync_molvest_reference_task,
            check_uip_reserve_task,
            check_uip_burn_task,
            register_reserved_uips_task,
            archive_old_codes_task,
        )

        task_map = {
            'refresh_suz_token': refresh_suz_token_task,
            'cleanup_expired_reserved': cleanup_expired_reserved_uips_task,
            'close_unused_registered': close_unused_registered_uips_task,
            'archive_stale_closed': archive_stale_closed_uips_task,
            'cleanup_old_logs': cleanup_old_logs_task,
            'sync_external_parties_codes': sync_external_parties_codes_task,
            'sync_molvest_reference': sync_molvest_reference_task,
            'check_uip_reserve': check_uip_reserve_task,
            'check_uip_burn': check_uip_burn_task,
            'register_reserved_uips': register_reserved_uips_task,
            'archive_old_codes': archive_old_codes_task,
        }

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
        })

    def _format_interval(self, seconds: int) -> str:
        if seconds < 60:
            return f'{seconds} сек'
        if seconds < 3600:
            return f'{seconds // 60} мин'
        if seconds < 86400:
            return f'{seconds // 3600} ч'
        days = seconds // 86400
        return f'{days} дн'
