# app_helper/middleware.py

import logging
import time

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import connection
from django.http import Http404

from app_helper import metrics
from app_helper.load_tracker import (
    TRAFFIC_BOT,
    TRAFFIC_MONITORING,
    TRAFFIC_USER,
    record_request,
)

logger = logging.getLogger(__name__)

# Не учитываем совсем: статика, медиа, фавикон.
IGNORED_PREFIXES = (
    '/static/',
    '/media/',
    '/templates/',
    '/favicon.ico',
)

# Служебные опросы собственного UI и мониторинга: учитываются отдельным
# классом `monitoring`, чтобы не раздувать пользовательскую нагрузку.
MONITORING_PREFIXES = (
    '/health/',
    '/helper/health/',
    '/helper/api/v1/status_service/',
    '/scheduler/status/',
    '/cz/nk/api/progress/',
    '/monitoring/',
    '/api/schema/',
    '/api/docs/',
)

# Признаки автоматических клиентов в User-Agent.
BOT_USER_AGENT_MARKERS = (
    'bot',
    'crawler',
    'spider',
    'scraper',
    'curl',
    'wget',
    'python-requests',
    'python-urllib',
    'httpclient',
    'go-http-client',
    'uptime',
    'nagios',
    'zabbix',
    'prometheus',
    'kube-probe',
    'headlesschrome',
    'lighthouse',
)


def classify_request(request) -> str:
    """Класс трафика: monitoring / bot / user."""
    path = request.path or ''
    if path.startswith(MONITORING_PREFIXES):
        return TRAFFIC_MONITORING

    user_agent = (request.META.get('HTTP_USER_AGENT') or '').lower()
    if any(marker in user_agent for marker in BOT_USER_AGENT_MARKERS):
        return TRAFFIC_BOT

    return TRAFFIC_USER


def status_class(status_code: int) -> str:
    """2xx / 3xx / 4xx / 5xx (или other для нестандартного кода)."""
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return 'other'
    if 100 <= code < 600:
        return f'{code // 100}xx'
    return 'other'


def request_group(request) -> str:
    """Имя endpoint для метрики: url_name/view_name или 1-й сегмент пути."""
    match = getattr(request, 'resolver_match', None)
    if match is not None:
        name = match.url_name or match.view_name
        if name:
            return name[:100]

    parts = [part for part in (request.path or '').split('/') if part]
    return (parts[0] if parts else 'root')[:100]


class LoadTrackingMiddleware:
    """
    Считает входящие запросы, время ответа и запросы к БД.

    - live-счётчики (`load_tracker`) — мгновенная нагрузка в процессе;
    - персистентные метрики (`app_helper.metrics`) — минутные агрегаты в БД
      для дашборда, истории и health-проверок.

    Служебные опросы (health, /scheduler/status/, прогресс НК) попадают
    в отдельный класс и не входят в пользовательскую нагрузку.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(IGNORED_PREFIXES):
            return self.get_response(request)

        traffic_class = classify_request(request)
        record_request(traffic_class)

        started = time.perf_counter()
        db_queries = 0
        db_ms = 0.0
        status_code = 500

        def _db_wrapper(execute, sql, params, many, context):
            nonlocal db_queries, db_ms
            begin = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db_queries += 1
                db_ms += (time.perf_counter() - begin) * 1000

        metrics.start_request()
        try:
            with connection.execute_wrapper(_db_wrapper):
                response = self.get_response(request)
            status_code = getattr(response, 'status_code', 500)
            return response
        except Http404:
            # Django превратит это в 404 уже после нашего middleware.
            status_code = 404
            raise
        except PermissionDenied:
            status_code = 403
            raise
        except SuspiciousOperation:
            status_code = 400
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            metrics.finish_request()
            try:
                metrics.record_request_metric(
                    group=request_group(request),
                    method=request.method,
                    status_class=status_class(status_code),
                    traffic_class=traffic_class,
                    duration_ms=duration_ms,
                    db_ms=db_ms,
                    db_queries=db_queries,
                )
            except Exception:  # noqa: BLE001 — метрики не должны ломать ответ
                logger.exception('Не удалось записать метрику запроса')
