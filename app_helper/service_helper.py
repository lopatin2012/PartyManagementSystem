# app_helper/service_helper.py

import logging

from django.utils import timezone
from django.db import connection

from app_helper.load_tracker import get_load_stats

logger = logging.getLogger(__name__)


def diagnose_service() -> dict:
    """
    Самодиагностика сервиса: проверяет БД, внешние зависимости и нагрузку.
    Возвращает итоговую доступность и детали по каждой проверке.
    """
    checks = {}

    # 1. Доступность БД.
    try:
        connection.ensure_connection()
        checks['database'] = {'ok': True, 'message': 'База данных доступна'}
    except Exception as e:
        logger.error(f'Самодиагностика: БД недоступна: {e}')
        checks['database'] = {'ok': False, 'message': str(e)}

    # 2. СУЗ (интеграция с Честным Знаком).
    try:
        from app_cz.models import SUZAccount
        account = SUZAccount.objects.filter(is_active=True).first()
        if account is None:
            checks['suz'] = {
                'ok': False,
                'message': 'Активная учётная запись СУЗ не настроена'
            }
        elif not account.dynamic_token or account.token_expires_at < timezone.now():
            checks['suz'] = {
                'ok': False,
                'message': 'Динамический токен СУЗ отсутствует'
            }
        else:
            checks['suz'] = {
                'ok': True,
                'message': 'СУЗ настроен, токен присутствует'
            }
    except Exception as e:
        logger.error(f'Самодиагностика: ошибка проверки СУЗ: {e}')
        checks['suz'] = {'ok': False, 'message': str(e)}

    # 3. Нагрузка.
    load = get_load_stats()
    checks['load'] = {
        'ok': not load['is_high_load'],
        **load,
    }

    is_available = all(c['ok'] for c in checks.values())

    return {
        'is_available': is_available,
        'checks': checks,
    }


