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
import time

import requests

from app_helper import metrics

logger = logging.getLogger(__name__)


def _outcome(response) -> str:
    status = getattr(response, 'status_code', 0) or 0
    if status >= 500:
        return '5xx'
    if status >= 400:
        return '4xx'
    return 'ok'


def _record(service: str, method: str, outcome: str,
            started: float) -> None:
    try:
        metrics.record_external_call(
            service=service,
            method=method,
            outcome=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception:  # noqa: BLE001 — метрики не должны ломать вызов
        logger.exception('Не удалось записать метрику внешнего вызова')


def request(service: str, method: str, url: str, **kwargs):
    """HTTP-запрос с замером; исключения `requests` пробрасываются."""
    started = time.perf_counter()
    try:
        response = requests.request(method, url, **kwargs)
    except requests.exceptions.Timeout:
        _record(service, method, 'timeout', started)
        raise
    except requests.exceptions.RequestException:
        _record(service, method, 'error', started)
        raise
    _record(service, method, _outcome(response), started)
    return response


def get(service: str, url: str, **kwargs):
    return request(service, 'GET', url, **kwargs)


def post(service: str, url: str, **kwargs):
    return request(service, 'POST', url, **kwargs)
