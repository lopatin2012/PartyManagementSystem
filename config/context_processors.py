# config/context_processors.py

from django.utils import timezone

from django.conf import settings

from app_helper.user_helper import get_user_name
from app_helper.load_tracker import get_requests_per_hour
from app_helper.service_helper import check_factories, check_onec
from app_cz.models import SUZAccount
from config.settings import SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION, DEBUG


def service_context(request):
    """Информация о работе сервиса."""
    return {
        'debug': getattr(settings, 'DEBUG', DEBUG),
    }


def user_context(request):
    """Информация о пользователе."""
    return {
        'user_name': get_user_name(request),
        'is_authenticated': request.user.is_authenticated,
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
    # 1. Статус СУЗ
    suz_account = SUZAccount.objects.filter(is_active=True).first()
    suz_status = {
        'is_active': bool(suz_account),
        'token_valid': False,
        'expires_in_seconds': 0,
        'can_manage': request.user.is_authenticated and request.user.is_superuser
    }

    if suz_account and suz_account.is_token_valid:
        suz_status['token_valid'] = True
        delta = suz_account.token_expires_at - timezone.now()
        suz_status['expires_in_seconds'] = max(0, int(delta.total_seconds()))

    # 2. Серверы маркировки заводов (Молвест.Маркировка) — реальная проверка.
    status_factories = check_factories()

    # 3. 1С: Предприятие — реальная проверка.
    status_1c = check_onec()

    return {
        'status_suz': suz_status,
        'status_factories': status_factories,
        'status_1c': status_1c,
    }
