# config/context_processors.py

from django.utils import timezone

from django.conf import settings

from app_cz.models import SUZAccount
from config.settings import SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION, DEBUG


def global_footer_info(request):
    """
    Добавляем информацию в контекст в шаблоны.
    :param request:
    :return:
    """
    requests_count = 1000

    return {
        'debug': DEBUG,
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

    return {
        'status_suz': suz_status,
        'status_factories': {
            'is_ok': True,
            'workshops': [
                {'name': 'ПАО МКВ', 'is_ok': True},
                {'name': 'Малыш', 'is_ok': True},
            ]
        },
        'status_1c': {
            'is_ok': True,
            'message': '1С: Предприятие'
        }
    }
