# app_event/services/health.py

"""
Наблюдаемость: периодическая проверка состояния системы и алерты.

- `run_health_checks()` прогоняет проверки (БД, СУЗ, подписи, заводы),
  пишет историю в `HealthCheck`.
- При смене состояния сервиса (ok → fail, fail → ok) пишет `EventLog` и
  шлёт письмо получателям группы «Мониторинг» (антидребезг: без повторов
  на каждый прогон).
- `cleanup_old_health_checks()` удаляет записи старше HEALTH_RETENTION_DAYS.
"""

import logging

from django.conf import settings
from django.utils import timezone

from app_event.models import HealthCheck, NotificationRecipient
from app_event.utils import log_event

logger = logging.getLogger(__name__)

# Соответствие код проверки → модуль EventLog.
_SERVICE_MODULE = {
    'database': 'system',
    'suz': 'cz',
    'signatures': 'cz',
    'factories': 'cz',
    'load': 'system',
    'summary': 'system',
}


def _health_enabled() -> bool:
    return bool(getattr(settings, 'HEALTH_CHECK_ENABLED', True))


def _alerts_enabled() -> bool:
    return bool(getattr(settings, 'SYSTEM_ALERTS_ENABLED', True))


def _retention_days() -> int:
    return int(getattr(settings, 'HEALTH_RETENTION_DAYS', 28) or 28)


def get_alert_recipients() -> list:
    """
    Email-адреса активных пользователей групп, настроенных на рассылку.

    Адреса берутся из `User.email`; пустые и дубликаты отбрасываются.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    groups = NotificationRecipient.objects.filter(is_active=True).values_list(
        'group_id', flat=True,
    )
    emails = (
        User.objects
        .filter(is_active=True, groups__id__in=list(groups))
        .exclude(email='')
        .values_list('email', flat=True)
        .distinct()
    )
    return list(emails)


def _send_alert(subject: str, body: str) -> dict:
    """Ставит письмо в очередь (emails) получателям групп рассылки."""
    recipients = get_alert_recipients()
    if not recipients:
        logger.warning(
            f'Алерт «{subject}»: нет адресатов — у групп рассылки '
            f'нет активных пользователей с заполненным email.'
        )
        return {'sent': False, 'reason': 'Нет получателей'}

    from app_scheduler.tasks import send_email_task
    try:
        send_email_task.enqueue(
            subject=subject, message=body, recipient_list=recipients,
        )
        return {'sent': True, 'recipients': recipients}
    except Exception as e:
        logger.error(f'Ошибка постановки письма-алерта «{subject}»: {e}')
        return {'sent': False, 'reason': str(e)}


def _last_state(service: str):
    """Последняя (до текущей) запись состояния сервиса."""
    return (
        HealthCheck.objects
        .filter(service=service)
        .order_by('-checked_at')
        .first()
    )


def _diagnose() -> dict:
    """Возвращает {service: {'name', 'ok', 'message', 'details'}}."""
    from app_helper.service_helper import (
        check_factories, check_signatures, check_suz_token,
    )
    from django.db import connection
    from app_helper.load_tracker import get_load_stats

    checks = {}

    try:
        connection.ensure_connection()
        checks['database'] = {
            'name': 'База данных', 'ok': True, 'message': 'Доступна',
        }
    except Exception as e:
        checks['database'] = {
            'name': 'База данных', 'ok': False, 'message': str(e),
        }

    suz = check_suz_token()
    checks['suz'] = {'name': 'СУЗ', 'ok': suz['is_ok'], 'message': suz['message']}

    sig = check_signatures()
    checks['signatures'] = {
        'name': 'Сервис подписей', 'ok': sig['is_ok'], 'message': sig['message'],
    }

    factories = check_factories()
    checks['factories'] = {
        'name': 'Серверы заводов',
        'ok': factories['is_ok'],
        'message': (
            'Все заводы доступны' if factories['is_ok']
            else 'Не все заводы доступны'
        ),
        'details': {'items': factories['factories']},
    }

    load = get_load_stats()
    checks['load'] = {
        'name': 'Нагрузка',
        'ok': not load['is_high_load'],
        'message': 'Нагрузка в норме' if not load['is_high_load'] else 'Высокая нагрузка',
        'details': load,
    }

    # Общая сводка: успешны ли все проверки.
    failed = [name for name, info in checks.items() if not info['ok']]
    checks['summary'] = {
        'name': 'Общая сводка',
        'ok': not failed,
        'message': (
            'Все проверки пройдены'
            if not failed
            else f'Проблемы: {", ".join(failed)}'
        ),
        'details': {
            'checks_total': len(checks),
            'checks_failed': len(failed),
            'failed': failed,
        },
    }

    return checks


def run_health_checks() -> dict:
    """
    Прогоняет проверки, сохраняет историю и шлёт алерты при смене состояния.

    :return: сводка выполнения.
    """
    if not _health_enabled():
        return {'is_error': False, 'skipped': True, 'message': 'Проверки отключены.'}

    checks = _diagnose()
    summary = {
        'is_error': False,
        'checked': 0,
        'failed': [],
        'alerts': [],
        'message': '',
    }

    for service, info in checks.items():
        previous = _last_state(service)
        was_ok = previous.is_ok if previous else None

        level = 'ok' if info['ok'] else 'critical'
        HealthCheck.objects.create(
            service=service,
            name=info['name'],
            is_ok=info['ok'],
            level=level,
            message=(info.get('message') or '')[:255],
            details=info.get('details') or {},
        )
        summary['checked'] += 1

        # Алерт при смене состояния. При первом наблюдении алертим только
        # о сбое (иначе система на старте засыпала бы письмами об OK).
        if was_ok is not None and was_ok == info['ok']:
            continue
        if was_ok is None and info['ok']:
            continue

        summary['failed'].append(service)
        module = _SERVICE_MODULE.get(service, 'system')
        if info['ok']:
            log_event(
                module=module, level='success',
                message=f'{info["name"]}: доступность восстановлена',
                metadata={'service': service},
            )
            if _alerts_enabled():
                _send_alert(
                    f'ВОССТАНОВЛЕНО: {info["name"]}',
                    f'Сервис «{info["name"]}» снова доступен.\n{info.get("message", "")}',
                )
        else:
            log_event(
                module=module, level='critical',
                message=f'{info["name"]}: недоступен — {info.get("message", "")}',
                metadata={'service': service},
            )
            if _alerts_enabled():
                sent = _send_alert(
                    f'СБОЙ: {info["name"]}',
                    f'Сервис «{info["name"]}» недоступен.\n{info.get("message", "")}',
                )
                summary['alerts'].append({'service': service, **sent})

    summary['is_error'] = bool(summary['failed'])
    summary['message'] = (
        f'Проверок: {summary["checked"]}, изменений состояния: '
        f'{len(summary["failed"])}.'
    )
    logger.info(summary['message'])
    return summary


def cleanup_old_health_checks() -> dict:
    """Удаляет записи HealthCheck старше HEALTH_RETENTION_DAYS."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=_retention_days())
    deleted, _ = HealthCheck.objects.filter(checked_at__lt=cutoff).delete()
    message = f'Очищено записей HealthCheck: {deleted} (старше {_retention_days()} дн.)'
    logger.info(message)
    return {'deleted': deleted, 'message': message}
