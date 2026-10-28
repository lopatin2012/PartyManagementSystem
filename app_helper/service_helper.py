# app_helper/service_helper.py

import logging
import time

import requests

from app_helper import external_http
from django.core.cache import cache
from django.utils import timezone
from django.db import connection

from app_helper.metrics import external_check, get_load_stats, queue_check

logger = logging.getLogger(__name__)

# Время жизни кэша результатов проверки сервисов (секунды),
# чтобы не «пинговать» внешние сервисы на каждом запросе.
SERVICE_CHECK_CACHE_TTL = 30

# Таймаут запроса при проверке доступности сервиса.
SERVICE_CHECK_TIMEOUT = 3


# Не спамим одинаковой ошибкой проверки доступности чаще, чем раз в 5 минут
# (иначе упавший завод даёт warning на каждом 30-секундном прозвоне).
FAILURE_LOG_INTERVAL = 300
_failure_log_at = {}


def _log_check_failure(url: str, error) -> None:
    now = time.monotonic()
    if now - _failure_log_at.get(url, 0) < FAILURE_LOG_INTERVAL:
        return
    _failure_log_at[url] = now
    logger.warning(f'Проверка доступности {url} не удалась: {error}')


def check_url_status(url: str, service: str = 'probe') -> dict:
    """
    Проверяет доступность URL (ожидается HTTP 2xx). Результат кэшируется.

    :param url: Полный адрес для проверки (например, http://192.168.1.50:8000/).
    :param service: код сервиса для метрик (factory/sign/1c/probe).
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
        response = external_http.get(
            service,
            url,
            timeout=SERVICE_CHECK_TIMEOUT,
            headers={'Accept': 'application/json'},
        )
        if 200 <= response.status_code < 300:
            result = {'is_ok': True, 'message': f'HTTP {response.status_code}'}
            _failure_log_at.pop(url, None)
        else:
            result = {'is_ok': False, 'message': f'HTTP {response.status_code}'}
    except requests.exceptions.RequestException as e:
        _log_check_failure(url, e)
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
            status = check_url_status(url, service='probe')
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
    return check_url_status(url, service='probe')


def check_db_connections() -> dict:
    """
    Загрузка соединений Postgres (для health-чека «Соединения БД»).

    :return: {'is_ok': bool, 'message': str}
    """
    from django.conf import settings

    try:
        used, maximum = _db_connection_counts()
    except Exception as e:
        logger.error(f'Проверка соединений Postgres: {e}')
        return {'is_ok': False, 'message': f'Не удалось получить соединения: {e}'}

    threshold = float(
        getattr(settings, 'DB_CONNECTIONS_THRESHOLD', 0.8) or 0.8
    )
    if maximum <= 0:
        return {'is_ok': True, 'message': f'Подключений PG: {used}'}

    share = used / maximum
    return {
        'is_ok': share < threshold,
        'message': (
            f'Соединений PG: {used} из {maximum} '
            f'({share:.0%}, порог {threshold:.0%})'
        ),
    }


def _db_connection_counts() -> tuple:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity")
        used = cursor.fetchone()[0] or 0
        cursor.execute("SELECT current_setting('max_connections')::int")
        maximum = cursor.fetchone()[0] or 0
    return used, maximum


def check_signatures() -> dict:
    """
    Проверяет доступность внешнего сервиса подписей.

    :return: {'is_ok': bool, 'message': str}
    """
    from django.conf import settings

    url = getattr(settings, 'SIGNATURE_SERVICE_URL', '') or ''
    if url:
        return check_url_status(url.rstrip('/') + '/', service='probe')

    # Адрес из модели ExternalService (тип «Сервис подписей»).
    try:
        from config.models import ExternalService, TypeServiceChoices
        service = ExternalService.objects.filter(
            service_type=TypeServiceChoices.SIGNATURE, is_active=True,
        ).first()
        if not service:
            return {'is_ok': False, 'message': 'Сервис подписей не настроен'}
        return check_url_status(
            f'http://{service.ip_address}:{service.port_address}/',
            service='probe',
        )
    except Exception as e:
        logger.error(f'Проверка сервиса подписей: {e}')
        return {'is_ok': False, 'message': str(e)}


def check_suz_token() -> dict:
    """
    Проверяет активную учётную запись СУЗ и срок действия динамического токена.

    :return: {'is_ok': bool, 'message': str}
    """
    try:
        from app_cz.models import SUZAccount
        account = SUZAccount.objects.filter(is_active=True).first()
        if account is None:
            return {'is_ok': False, 'message': 'Активная учётная запись СУЗ не настроена'}
        if not account.dynamic_token:
            return {'is_ok': False, 'message': 'Динамический токен СУЗ отсутствует'}
        if account.token_expires_at and account.token_expires_at < timezone.now():
            return {'is_ok': False, 'message': 'Динамический токен СУЗ истёк'}
        expires = (
            account.token_expires_at.strftime('%Y-%m-%d %H:%M')
            if account.token_expires_at else 'без срока'
        )
        return {'is_ok': True, 'message': f'Токен действителен до {expires}'}
    except Exception as e:
        logger.error(f'Проверка СУЗ: {e}')
        return {'is_ok': False, 'message': str(e)}


def diagnose_service() -> dict:
    """
    Самодиагностика сервиса: проверяет БД, внешние зависимости и нагрузку.
    Возвращает итоговую доступность и детали по каждой проверке.
    """
    checks = {}

    # 1. Доступность БД.
    try:
        connection.ensure_connection()
        checks['database'] = {
            'name': 'База данных',
            'ok': True,
            'message': 'База данных доступна',
        }
    except Exception as e:
        logger.error(f'Самодиагностика: БД недоступна: {e}')
        checks['database'] = {
            'name': 'База данных',
            'ok': False,
            'message': str(e),
        }

    # 2. Загрузка соединений Postgres.
    db_connections = check_db_connections()
    checks['db_load'] = {
        'name': 'Соединения БД',
        'ok': db_connections['is_ok'],
        'message': db_connections['message'],
    }

    # 3. СУЗ (интеграция с Честным Знаком).
    suz = check_suz_token()
    checks['suz'] = {
        'name': 'СУЗ (Честный Знак)',
        'ok': suz['is_ok'],
        'message': suz['message'],
    }

    # 3. Сервис подписей.
    sig = check_signatures()
    checks['signatures'] = {
        'name': 'Сервис подписей',
        'ok': sig['is_ok'],
        'message': sig['message'],
    }

    # 4. Серверы заводов (Молвест.Маркировка).
    factories = check_factories()
    items = factories['factories']
    ok_factories = sum(1 for item in items if item['is_ok'])
    checks['factories'] = {
        'name': 'Заводы (Молвест.Маркировка)',
        'ok': factories['is_ok'],
        'message': (
            'Заводы не настроены'
            if not items
            else (
                f'Доступны {ok_factories} из {len(items)} заводов'
                if factories['is_ok']
                else f'Доступны не все заводы: {ok_factories} из {len(items)}'
            )
        ),
        'items': items,
    }

    # 5. Исходящие вызовы (ЧЗ, НК, СУЗ, подписи, заводы) — доля ошибок.
    checks['external'] = external_check()

    # 6. Очередь фоновых задач (django-tasks-db).
    checks['queue'] = queue_check()

    # 7. Нагрузка.
    # Счётчики живут в памяти web-процесса, поэтому в воркере данных нет.
    # Это не сбой, но и не «в норме» — честно показываем «нет данных».
    load = get_load_stats()
    has_load_data = load.get('has_data', True)
    checks['load'] = {
        'name': 'Нагрузка',
        'ok': (not load['is_high_load']) if has_load_data else True,
        **load,
    }

    # 8. Общая сводка по сервису.
    failed = [name for name, c in checks.items() if not c['ok']]
    failed_names = [checks[name].get('name', name) for name in failed]
    checks['summary'] = {
        'name': 'Общая сводка',
        'ok': not failed,
        'message': (
            'Все проверки пройдены'
            if not failed
            else f'Проблемы: {", ".join(failed_names)}'
        ),
        'checks_total': len(checks),
        'checks_failed': len(failed),
    }

    is_available = not failed

    return {
        'is_available': is_available,
        'checks': checks,
    }


