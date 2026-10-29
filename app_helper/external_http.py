# app_helper/external_http.py

"""
Исходящие HTTP-вызовы с метриками.

Тонкая обёртка над `requests`, которая замеряет длительность и фиксирует
сервис/метод/исход в `app_helper/metrics.py` (`app_event.ExternalCallMetric`).

Использование:

    from app_helper import external_http
    response = external_http.get('cz', url, timeout=30)
    response = external_http.post('suz', url, json=payload, timeout=30)

`service` — короткий код: `cz`, `nk`, `suz`, `sign`, `factory`, `1c`.
"""

import logging
import re
import time
from urllib.parse import urlsplit

import requests

from app_helper import metrics

logger = logging.getLogger(__name__)

# Идентификаторы в пути заменяются на {id}, чтобы метрика не разрасталась
# по одному только uuid задания/кода.
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)
_HEX_ID_RE = re.compile(r'^[0-9a-fA-F]{16,}$')
_NUMBER_RE = re.compile(r'^\d+$')


def endpoint_label(url: str) -> str:
    """
    Короткий «шаблон» пути запроса для метрик.

    Пример: `/codes/api/get_any_code_for_task/0f8b…/` →
    `/codes/api/get_any_code_for_task/{id}/`.
    """
    try:
        path = urlsplit(str(url)).path or '/'
    except ValueError:
        path = str(url)

    parts = []
    for segment in path.strip('/').split('/'):
        if not segment:
            continue
        if (
            _UUID_RE.match(segment)
            or _HEX_ID_RE.match(segment)
            or _NUMBER_RE.match(segment)
        ):
            parts.append('{id}')
        else:
            parts.append(segment)

    if not parts:
        return '/'
    label = '/' + '/'.join(parts)
    if path.endswith('/'):
        label += '/'
    return label[:120]


def _outcome(response) -> str:
    """Исход вызова: `ok` или точный HTTP-код ошибки (404, 500, …)."""
    status = getattr(response, 'status_code', 0) or 0
    if status >= 400:
        return str(status)
    return 'ok'


def _record(service: str, method: str, outcome: str,
            started: float, endpoint: str = '') -> None:
    try:
        metrics.record_external_call(
            service=service,
            method=method,
            outcome=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
            endpoint=endpoint,
        )
    except Exception:  # noqa: BLE001 — метрики не должны ломать вызов
        logger.exception('Не удалось записать метрику внешнего вызова')


def request(service: str, method: str, url: str, **kwargs):
    """HTTP-запрос с замером; исключения `requests` пробрасываются."""
    started = time.perf_counter()
    endpoint = endpoint_label(url)
    try:
        response = requests.request(method, url, **kwargs)
    except requests.exceptions.Timeout:
        _record(service, method, 'timeout', started, endpoint)
        raise
    except requests.exceptions.RequestException:
        _record(service, method, 'error', started, endpoint)
        raise
    _record(service, method, _outcome(response), started, endpoint)
    return response


def get(service: str, url: str, **kwargs):
    return request(service, 'GET', url, **kwargs)


def post(service: str, url: str, **kwargs):
    return request(service, 'POST', url, **kwargs)
