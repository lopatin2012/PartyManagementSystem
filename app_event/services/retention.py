# app_event/services/retention.py

"""
Сроки хранения логов и метрик + их очистка.

Единое место, где видно, сколько хранится каждая «логовая» таблица и чем она
чистится. Используется scheduled-задачами (`app_scheduler/tasks.py`) и
командой `python manage.py cleanup_logs`.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _days(setting_name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, setting_name, default)))
    except (TypeError, ValueError):
        return default


def event_log_retention_days() -> int:
    return _days('EVENT_LOG_RETENTION_DAYS', 90)


def uip_status_log_retention_days() -> int:
    return _days('UIP_STATUS_LOG_RETENTION_DAYS', 365)


def task_result_retention_days() -> int:
    return _days('TASK_RESULT_RETENTION_DAYS', 30)


def _delete_older(model, field: str, days: int, label: str) -> dict:
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _details = model.objects.filter(**{f'{field}__lt': cutoff}).delete()
    message = f'Очищено {label}: {deleted} (старше {days} дн.)'
    logger.info(message)
    return {'table': label, 'deleted': deleted, 'days': days, 'message': message}


def cleanup_old_events(days: int = None) -> dict:
    """Удалить события EventLog старше EVENT_LOG_RETENTION_DAYS."""
    from app_event.models import EventLog

    return _delete_older(
        EventLog, 'created_at', days or event_log_retention_days(), 'EventLog',
    )


def cleanup_old_uip_status_logs(days: int = None) -> dict:
    """Удалить историю статусов УИП старше UIP_STATUS_LOG_RETENTION_DAYS."""
    from app_uip.models import UIPStatusLog

    return _delete_older(
        UIPStatusLog, 'created_at',
        days or uip_status_log_retention_days(), 'UIPStatusLog',
    )


def cleanup_old_task_results(days: int = None) -> dict:
    """Удалить завершённые DBTaskResult старше TASK_RESULT_RETENTION_DAYS."""
    from django_tasks_db.models import DBTaskResult

    return _delete_older(
        DBTaskResult, 'finished_at',
        days or task_result_retention_days(), 'DBTaskResult',
    )


def cleanup_old_health_checks() -> dict:
    """HealthCheck чистится по HEALTH_RETENTION_DAYS (функция в services/health)."""
    from app_event.services.health import cleanup_old_health_checks as cleanup

    result = cleanup()
    return {
        'table': 'HealthCheck',
        'deleted': result.get('deleted', 0),
        'days': _days('HEALTH_RETENTION_DAYS', 14),
        'message': result.get('message', ''),
    }


def cleanup_metrics() -> dict:
    """Метрики запросов/внешних вызовов/инфраструктуры."""
    from app_helper.metrics import prune_old_metrics

    result = prune_old_metrics()
    return {
        'table': 'Metrics',
        'deleted': result.get('deleted', 0),
        'days': result.get('days', 14),
        'message': result.get('message', ''),
        'is_error': result.get('is_error', False),
    }


def cleanup_all() -> list:
    """Прогнать все очистки (команда/ручной запуск)."""
    results = [
        cleanup_old_events(),
        cleanup_old_health_checks(),
        cleanup_old_uip_status_logs(),
        cleanup_old_task_results(),
        cleanup_metrics(),
    ]
    return results


def retention_report() -> list:
    """
    Отчёт по «логовым» таблицам: сколько строк, самая старая запись, срок.

    Считает COUNT(*) по ORM — команда ручная, таблицы небольшие.
    """
    from django_tasks_db.models import DBTaskResult

    from app_event.models import (
        EventLog, ExternalCallMetric, HealthCheck, RequestMetric, SystemMetric,
    )
    from app_uip.models import UIPStatusLog
    from app_helper.metrics import retention_days as metrics_retention_days

    specs = [
        ('EventLog', EventLog.objects, 'created_at',
         event_log_retention_days()),
        ('HealthCheck', HealthCheck.objects, 'checked_at',
         _days('HEALTH_RETENTION_DAYS', 14)),
        ('UIPStatusLog', UIPStatusLog.objects, 'created_at',
         uip_status_log_retention_days()),
        ('DBTaskResult', DBTaskResult.objects, 'enqueued_at',
         task_result_retention_days()),
        ('RequestMetric', RequestMetric.objects, 'bucket_start',
         metrics_retention_days()),
        ('ExternalCallMetric', ExternalCallMetric.objects, 'bucket_start',
         metrics_retention_days()),
        ('SystemMetric', SystemMetric.objects, 'bucket_start',
         metrics_retention_days()),
    ]

    report = []
    for label, queryset, date_field, days in specs:
        count = queryset.count()
        oldest = (
            queryset.order_by(date_field)
            .values_list(date_field, flat=True)
            .first()
        )
        report.append({
            'table': label,
            'rows': count,
            'oldest': oldest,
            'retention_days': days,
        })
    return report
