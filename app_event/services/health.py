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


def _send_alert(subject: str, body: str, html_message: str = None) -> dict:
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
            html_message=html_message,
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


def _confirm_attempts() -> int:
    """Сколько подряд неудачных проверок подтверждают сбой. Минимум 1."""
    return max(1, int(getattr(settings, 'HEALTH_CONFIRM_ATTEMPTS', 3) or 3))


def _consecutive_failures_series(service: str) -> int:
    """Сколько неудачных проверок подряд накопилось до текущего результата."""
    count = 0
    for is_ok in (
        HealthCheck.objects
        .filter(service=service)
        .values_list('is_ok', flat=True)
        .order_by('-checked_at')[:1000]
    ):
        if is_ok:
            break
        count += 1
    return count


def _failure_since(service: str):
    """Время самой ранней проверки из текущей (последней) серии сбоев."""
    since = None
    for row in (
        HealthCheck.objects
        .filter(service=service)
        .order_by('-checked_at')
        .values('is_ok', 'checked_at')[:1000]
    ):
        if row['is_ok']:
            break
        since = row['checked_at']
    return since


def _fmt_time(dt) -> str:
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime('%d.%m.%Y %H:%M:%S')


def _escape(value) -> str:
    from html import escape
    return escape(str(value), quote=True)


# ==========================================
# Оформление писем-алертов.
# ==========================================

_OK_COLOR = '#1e8e3e'
_FAIL_COLOR = '#d93025'
_MUTED = '#5f6368'
_TEXT = '#202124'
_BORDER = '#e0e0e0'


def _details_rows(details: dict) -> list:
    """Плоские строки «ключ: значение» из details (для письма)."""
    rows = []
    for key, value in (details or {}).items():
        if key in ('items', 'failed_names', 'failed'):
            continue
        if isinstance(value, (dict, list)):
            continue
        rows.append((key, value))
    return rows


def _build_alert_content(
        service: str, info: dict, *, recovered: bool,
        failures_count: int, since,
) -> tuple:
    """Собирает (text, html, summary_line) для письма-алерта."""
    name = info['name']
    message = info.get('message') or ''
    now = timezone.now()
    status_word = 'восстановлен' if recovered else 'недоступен'
    color = _OK_COLOR if recovered else _FAIL_COLOR
    stamp = _fmt_time(now)

    rows = [('Сервис', name)]
    if recovered:
        rows.append(('Статус', 'Доступен'))
        rows.append(('Время восстановления', stamp))
        if failures_count:
            rows.append(('Сбоев подряд перед восстановлением', str(failures_count)))
    else:
        rows.append(('Статус', 'Недоступен'))
        rows.append(('Время фиксации', stamp))
        if failures_count:
            rows.append(('Неудачных проверок подряд', str(failures_count)))
    if since is not None:
        rows.append(('Сбой с', _fmt_time(since)))
    rows.append(('Сообщение', message))
    for key, value in _details_rows(info.get('details') or {}):
        if value in (None, ''):
            continue
        rows.append((str(key), str(value)))

    factory_rows = []
    details = info.get('details') or {}
    for item in details.get('items') or []:
        factory_rows.append((
            item.get('name', '—'),
            'доступен' if item.get('is_ok')
            else f'недоступен ({item.get("message", "")})',
            item.get('is_ok', False),
        ))

    # Текстовое представление.
    text_lines = [f'Сервис «{name}» {status_word}.', '']
    width = max((len(k) for k, _ in rows), default=0)
    for key, value in rows:
        text_lines.append(f'{key.ljust(width)} : {value}')
    if factory_rows:
        text_lines.append('')
        text_lines.append('Заводы:')
        for fname, fstatus, _ in factory_rows:
            text_lines.append(f'  - {fname}: {fstatus}')
    text_lines.append('')
    text_lines.append(f'Система мониторинга PartyManagementSystem · {stamp}')
    text = '\n'.join(text_lines)

    # HTML.
    html_rows = ''.join(
        f'<tr>'
        f'<td style="padding:6px 12px;color:{_MUTED};white-space:nowrap;'
        f'vertical-align:top">{_escape(key)}</td>'
        f'<td style="padding:6px 12px;color:{_TEXT};font-weight:600">'
        f'{_escape(value)}</td></tr>'
        for key, value in rows
    )
    factories_html = ''
    if factory_rows:
        items_html = ''.join(
            f'<li style="color:{_TEXT if ok else _FAIL_COLOR}">'
            f'{_escape(fname)} — {_escape(fstatus)}</li>'
            for fname, fstatus, ok in factory_rows
        )
        factories_html = (
            f'<h3 style="font-size:14px;color:{_TEXT};margin:18px 0 6px">'
            f'Серверы заводов</h3>'
            f'<ul style="margin:0;padding-left:20px;font-size:13px">'
            f'{items_html}</ul>'
        )
    html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;color:{_TEXT};'
        f'max-width:640px">'
        f'<div style="background:{color};color:#fff;padding:14px 18px;'
        f'border-radius:8px 8px 0 0;font-size:16px;font-weight:700">'
        f'Сервис «{_escape(name)}» {_escape(status_word)}</div>'
        f'<div style="border:1px solid {_BORDER};border-top:none;'
        f'border-radius:0 0 8px 8px;padding:12px 6px">'
        f'<table style="border-collapse:collapse;width:100%;font-size:14px">'
        f'{html_rows}</table>'
        f'{factories_html}'
        f'<p style="color:{_MUTED};font-size:12px;margin:18px 12px 4px">'
        f'Система мониторинга PartyManagementSystem</p>'
        f'</div></div>'
    )

    summary_line = f'«{name}» {status_word}: {message}'
    return text, html, summary_line


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
            'name': 'База данных',
            'ok': True,
            'message': 'Подключение установлено',
            'details': {
                'engine': connection.vendor,
                'name': connection.settings_dict.get('NAME', ''),
                'host': connection.settings_dict.get('HOST', '') or 'localhost',
            },
        }
    except Exception as e:
        checks['database'] = {
            'name': 'База данных',
            'ok': False,
            'message': str(e),
            'details': {
                'name': connection.settings_dict.get('NAME', ''),
                'host': connection.settings_dict.get('HOST', '') or 'localhost',
            },
        }

    # Проактивно обновляем токен СУЗ заранее (за ~1 час до истечения),
    # т.к. проверка идёт каждые 5 минут — это закрывает разрыв между
    # редкими запусками задачи refresh_suz_token (6 ч при жизни токена 8 ч).
    try:
        from app_cz.services.suz_client import ensure_suz_token_valid
        refresh_result = ensure_suz_token_valid()
        if not refresh_result.get('skipped') and refresh_result.get('refreshed'):
            logger.info('Health-check: динамический токен СУЗ обновлён заранее.')
    except Exception as e:
        logger.warning(f'Health-check: не удалось проверить/обновить токен СУЗ: {e}')

    suz = check_suz_token()
    suz_details = {}
    try:
        from app_cz.models import SUZAccount
        account = SUZAccount.objects.filter(is_active=True).first()
        if account:
            suz_details = {
                'account': getattr(account, 'name', '') or getattr(account, 'inn', ''),
                'inn': getattr(account, 'inn', ''),
                'token_expires_at': (
                    account.token_expires_at.strftime('%d.%m.%Y %H:%M')
                    if account.token_expires_at else 'без срока'
                ),
            }
    except Exception:
        pass
    checks['suz'] = {
        'name': 'Честный Знак (СУЗ)',
        'ok': suz['is_ok'],
        'message': suz['message'],
        'details': suz_details,
    }

    sig = check_signatures()
    checks['signatures'] = {
        'name': 'Сервис подписей',
        'ok': sig['is_ok'],
        'message': sig['message'],
        'details': {'message': sig['message']},
    }

    factories = check_factories()
    items = factories['factories']
    total = len(items)
    ok_count = sum(1 for item in items if item['is_ok'])
    failed = [item for item in items if not item['is_ok']]
    if total == 0:
        factories_message = 'Нет активных заводов для проверки'
    elif ok_count == total:
        factories_message = f'Доступны все заводы ({ok_count}/{total})'
    else:
        factories_message = (
            f'Недоступно заводов: {total - ok_count} из {total}'
        )
    checks['factories'] = {
        'name': 'Серверы заводов (Молвест.Маркировка)',
        'ok': factories['is_ok'],
        'message': factories_message,
        'details': {
            'total': total,
            'ok_count': ok_count,
            'failed_count': total - ok_count,
            'items': items,
            'failed_names': [item['name'] for item in failed],
        },
    }

    load = get_load_stats()
    checks['load'] = {
        'name': 'Нагрузка',
        'ok': not load['is_high_load'],
        'message': (
            f'В норме: {load["requests_per_hour"]} запросов/час'
            if not load['is_high_load']
            else f'Высокая: {load["requests_per_hour"]} запросов/час '
                 f'(порог {load["threshold_per_hour"]})'
        ),
        'details': load,
    }

    # Общая сводка: успешны ли все проверки (кроме самой сводки).
    failed = [svc for svc, info in checks.items() if not info['ok']]
    failed_names = [checks[svc]['name'] for svc in failed]
    checks['summary'] = {
        'name': 'Общая сводка',
        'ok': not failed,
        'message': (
            f'Все проверки пройдены ({len(checks)}/{len(checks)})'
            if not failed
            else f'Проблемных сервисов: {len(failed)} — '
                 f'{", ".join(failed_names)}'
        ),
        'details': {
            'checks_total': len(checks),
            'checks_failed': len(failed),
            'failed': failed,
            'failed_names': failed_names,
        },
    }

    return checks


def run_health_checks() -> dict:
    """
    Прогоняет проверки, сохраняет историю и шлёт алерты при смене состояния.

    Сбой подтверждается только после `HEALTH_CONFIRM_ATTEMPTS` неудачных
    проверок подряд — одиночные «мигания» сети письма не шлют. Восстановление
    фиксируется сразу (один успешный прогон).

    :return: сводка выполнения.
    """
    if not _health_enabled():
        return {'is_error': False, 'skipped': True, 'message': 'Проверки отключены.'}

    checks = _diagnose()
    confirm = _confirm_attempts()
    summary = {
        'is_error': False,
        'checked': 0,
        'failed': [],
        'alerts': [],
        'message': '',
    }

    for service, info in checks.items():
        # Серия неудач ДО записи текущего результата.
        prior_failures = _consecutive_failures_series(service)
        failures_count = prior_failures + (0 if info['ok'] else 1)

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

        module = _SERVICE_MODULE.get(service, 'system')
        name = info['name']

        if info['ok']:
            # Восстановление: сервис был недоступен, а теперь доступен.
            # Считаем только подтверждённые сбои (серия >= порога), чтобы не
            # слать «восстановлено» после одиночного мигания.
            if prior_failures < confirm:
                continue
            summary['failed'].append(service)
            log_event(
                module=module, level='success',
                message=f'{name}: доступность восстановлена',
                metadata={'service': service, 'failures': prior_failures},
            )
            if _alerts_enabled():
                _send_alert_for(
                    service, info, recovered=True,
                    failures_count=prior_failures,
                    since=_failure_since(service),
                )
            continue

        # Сбой: алертим один раз — впервые, когда серия достигает порога.
        if failures_count != confirm:
            continue
        summary['failed'].append(service)
        log_event(
            module=module, level='critical',
            message=f'{name}: недоступен — {info.get("message", "")}',
            metadata={'service': service, 'failures': failures_count},
        )
        if _alerts_enabled():
            sent = _send_alert_for(
                service, info, recovered=False,
                failures_count=failures_count,
                since=_failure_since(service),
            )
            summary['alerts'].append({'service': service, **sent})

    summary['is_error'] = bool(summary['failed'])
    summary['message'] = (
        f'Проверок: {summary["checked"]}, изменений состояния: '
        f'{len(summary["failed"])}.'
    )
    logger.info(summary['message'])
    return summary


def _send_alert_for(
        service: str, info: dict, *, recovered: bool,
        failures_count: int, since,
) -> dict:
    """Собирает и отправляет письмо-алерт (текст + HTML)."""
    name = info['name']
    text, html, summary_line = _build_alert_content(
        service, info, recovered=recovered,
        failures_count=failures_count, since=since,
    )
    subject = (
        f'ВОССТАНОВЛЕНО: {name}' if recovered else f'СБОЙ: {name}'
    )
    return _send_alert(subject, text, html_message=html)


def cleanup_old_health_checks() -> dict:
    """Удаляет записи HealthCheck старше HEALTH_RETENTION_DAYS."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=_retention_days())
    deleted, _ = HealthCheck.objects.filter(checked_at__lt=cutoff).delete()
    message = f'Очищено записей HealthCheck: {deleted} (старше {_retention_days()} дн.)'
    logger.info(message)
    return {'deleted': deleted, 'message': message}
