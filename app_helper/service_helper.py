# app_helper/service_helper.py

import logging

import requests
from django.core.cache import cache
from django.utils import timezone
from django.db import connection

from app_helper.load_tracker import get_load_stats

logger = logging.getLogger(__name__)

# Время жизни кэша результатов проверки сервисов (секунды),
# чтобы не «пинговать» внешние сервисы на каждом запросе.
SERVICE_CHECK_CACHE_TTL = 30

# Таймаут запроса при проверке доступности сервиса.
SERVICE_CHECK_TIMEOUT = 3


def check_url_status(url: str) -> dict:
    """
    Проверяет доступность URL (ожидается HTTP 2xx). Результат кэшируется.

    :param url: Полный адрес для проверки (например, http://192.168.1.50:8000/).
    :return: {'is_ok': bool, 'message': str}
    """
    cache_key = f'service_check:{url}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not url:
        result = {'is_ok': False, 'message': 'Адрес не настроен'}
        cache.set(cache_key, result, SERVICE_CHECK_CACHE_TTL)
        return result

    try:
        response = requests.get(
            url,
            timeout=SERVICE_CHECK_TIMEOUT,
            headers={'Accept': 'application/json'},
        )
        if 200 <= response.status_code < 300:
            result = {'is_ok': True, 'message': f'HTTP {response.status_code}'}
        else:
            result = {'is_ok': False, 'message': f'HTTP {response.status_code}'}
    except requests.exceptions.RequestException as e:
        logger.warning(f'Проверка доступности {url} не удалась: {e}')
        result = {'is_ok': False, 'message': 'Ошибка соединения'}

    cache.set(cache_key, result, SERVICE_CHECK_CACHE_TTL)
    return result


def check_factories() -> dict:
    """
    Проверяет доступность серверов маркировки заводов (Молвест.Маркировка).

    Адрес каждого завода берётся из модели Factory (ip_address/port_address).
    :return: {'is_ok': bool, 'factories': [{'id', 'name', 'is_ok', 'message'}]}
    """
    from app_factory.models import Factory

    factories = Factory.objects.filter(is_active=True).order_by('name')
    items = []
    ok_count = 0

    for factory in factories:
        if factory.ip_address and factory.port_address:
            url = f'http://{factory.ip_address}:{factory.port_address}/'
            status = check_url_status(url)
        else:
            status = {'is_ok': False, 'message': 'Адрес не задан'}

        items.append({
            'id': str(factory.id),
            'name': factory.name,
            'is_ok': status['is_ok'],
            'message': status['message'],
        })
        if status['is_ok']:
            ok_count += 1

    return {
        'is_ok': bool(items) and ok_count == len(items),
        'factories': items,
    }


def check_onec() -> dict:
    """
    Проверяет доступность сервиса 1С (адрес из настройки ONEC_URL).
    :return: {'is_ok': bool, 'message': str}
    """
    from django.conf import settings

    url = getattr(settings, 'ONEC_URL', '')
    if not url:
        return {'is_ok': False, 'message': 'Адрес 1С не настроен'}
    return check_url_status(url)


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


