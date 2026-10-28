# config/context_processors.py

from django.utils import timezone

from django.conf import settings

from app_helper.user_helper import get_user_name
from app_helper.metrics import get_client_totals
from app_helper.service_helper import check_factories
from app_cz.models import SUZAccount
from config.settings import SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION, DEBUG

# Снимок health-проверок старше 30 минут считаем устаревшим и проверяем вживую.
STATUS_SNAPSHOT_MAX_AGE_SECONDS = 30 * 60


def service_context(request):
    """Информация о работе сервиса."""
    return {
        'debug': getattr(settings, 'DEBUG', DEBUG),
    }


def user_context(request):
    """Информация о пользователе и его правах (роли)."""
    from app_helper.access import (
        is_admin, is_viewer, can_view_uip, can_generate_uip,
    )

    user = request.user
    return {
        'user_name': get_user_name(request),
        'is_authenticated': user.is_authenticated,
        'is_admin': is_admin(user),
        'is_viewer': is_viewer(user),
        'can_view_uip': can_view_uip(user),
        'can_generate_uip': can_generate_uip(user),
    }


def global_footer_info(request):
    """
    Добавляем информацию в контекст в шаблоны.
    :param request:
    :return:
    """
    # Запросы за последние 60 минут (без служебных опросов UI/мониторинга).
    totals = get_client_totals()

    return {
        'service_version': getattr(settings, 'SERVICE_VERSION', SERVICE_VERSION),
        'requests_count': totals['requests'],
        'requests_peak': totals['peak'],
        'requests_monitoring': totals['monitoring'],
        'service_mode_name': getattr(settings, 'SERVICE_MODE_TEXT', SERVICE_MODE_TEXT),
        'service_mode_color': getattr(settings, 'SERVICE_MODE_COLOR', SERVICE_MODE_COLOR),
    }


def _latest_health_checks() -> dict:
    """
    Последняя проверка по каждому сервису (пишет воркер раз в 5 минут).

    Панель статусов читает этот снимок вместо живых внешних проверок на
    каждом рендере страницы — иначе каждая вкладка «пинговала» заводы,
    подписи и 1С (переизбыток запросов от мониторинга).

    Если снимок устарел (проверки давно не выполнялись), возвращаем пусто —
    панель сделает живую проверку, чтобы не показывать старый статус.
    """
    try:
        from app_event.models import HealthCheck

        rows = (
            HealthCheck.objects
            .order_by('service', '-checked_at')
            .distinct('service')
        )
        snapshot = {row.service: row for row in rows}
        if snapshot:
            newest = max(row.checked_at for row in snapshot.values())
            age = (timezone.now() - newest).total_seconds()
            if age > STATUS_SNAPSHOT_MAX_AGE_SECONDS:
                return {}
        return snapshot
    except Exception:  # noqa: BLE001 — нет таблицы/БД — используем fallback
        return {}


def service_status_info(request):
    """Статусы внешних сервисов."""
    from app_helper.access import is_admin

    # 1. Статус СУЗ (живой срок токена — только БД, без внешних запросов).
    suz_account = SUZAccount.objects.filter(is_active=True).first()
    suz_status = {
        'is_active': bool(suz_account),
        'token_valid': False,
        'expires_in_seconds': 0,
        'can_manage': is_admin(request.user),
    }

    if suz_account and suz_account.is_token_valid:
        suz_status['token_valid'] = True
        delta = suz_account.token_expires_at - timezone.now()
        suz_status['expires_in_seconds'] = max(0, int(delta.total_seconds()))

    # 2. Снимок последних health-проверок (refresh раз в 5 минут воркером).
    snapshot = _latest_health_checks()

    factories_check = snapshot.get('factories')
    if factories_check is not None and isinstance(
            factories_check.details, dict):
        status_factories = {
            'is_ok': bool(factories_check.is_ok),
            'factories': factories_check.details.get('items') or [],
        }
    else:
        # Первый запуск / нет истории проверок — живая проверка.
        status_factories = check_factories()

    if snapshot:
        failed_checks = [
            {'name': row.name, 'message': row.message or 'недоступно'}
            for service, row in snapshot.items()
            if service != 'summary' and not row.is_ok
        ]
        checks_total = len([s for s in snapshot if s != 'summary'])
        status_summary = {
            'is_ok': not failed_checks,
            'checks_total': checks_total,
            'checks_failed': len(failed_checks),
            'failed_checks': failed_checks,
            'message': (
                'Сервис работает штатно'
                if not failed_checks
                else 'Есть проблемы с сервисами'
            ),
        }
    else:
        # Фолбэк: health-истории ещё нет — живая самодиагностика.
        from app_helper.service_helper import diagnose_service

        diagnosis = diagnose_service()
        failed_checks = [
            {
                'name': info.get('name', name),
                'message': info.get('message', 'недоступно'),
            }
            for name, info in diagnosis['checks'].items()
            if not info['ok'] and name != 'summary'
        ]
        status_summary = {
            'is_ok': diagnosis['is_available'],
            'checks_total': len(diagnosis['checks']) - 1,  # без сводки
            'checks_failed': len(failed_checks),
            'failed_checks': failed_checks,
            'message': (
                'Сервис работает штатно'
                if diagnosis['is_available']
                else 'Есть проблемы с сервисами'
            ),
        }

    return {
        'status_suz': suz_status,
        'status_factories': status_factories,
        'status_summary': status_summary,
    }
