# config/context_processors.py

from django.utils import timezone

from django.conf import settings

from app_helper.user_helper import get_user_name
from app_helper.load_tracker import get_requests_per_hour
from app_helper.service_helper import check_factories
from app_cz.models import SUZAccount
from config.settings import SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION, DEBUG


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
    # Реальное количество запросов за последний час (считает middleware).
    requests_count = get_requests_per_hour()

    return {
        'service_version': getattr(settings, 'SERVICE_VERSION', SERVICE_VERSION),
        'requests_count': requests_count,
        'service_mode_name': getattr(settings, 'SERVICE_MODE_TEXT', SERVICE_MODE_TEXT),
        'service_mode_color': getattr(settings, 'SERVICE_MODE_COLOR', SERVICE_MODE_COLOR),
    }


def service_status_info(request):
    """Статусы внешних сервисов."""
    from app_helper.access import is_admin

    # 1. Статус СУЗ
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

    # 2. Серверы маркировки заводов (Молвест.Маркировка) — реальная проверка.
    status_factories = check_factories()

    # 3. Общая сводка по сервису (все проверки).
    from app_helper.service_helper import diagnose_service
    diagnosis = diagnose_service()
    failed_checks = [
        {
            'name': name,
            'message': info.get('message', 'недоступно'),
        }
        for name, info in diagnosis['checks'].items()
        if not info['ok'] and name != 'summary'
    ]
    status_summary = {
        'is_ok': diagnosis['is_available'],
        'checks_total': len(diagnosis['checks']) - 1,  # без самой сводки
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
