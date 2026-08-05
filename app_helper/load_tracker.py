# app_helper/load_tracker.py

import time
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Лимит запросов в час. При превышении — предупреждение в логах.
HIGH_LOAD_THRESHOLD = 10_000

BUCKET_SECONDS = 60  # размер бакета — 1 минута.
HOUR_BUCKETS = 60  # 60 минут.
CACHE_TTL = 2 * 3600  # ключи живут 2 часа.


def _current_minute_ts() -> int:
    """Timestamp начала текущей минуты."""
    now = int(time.time())
    return now - (now % BUCKET_SECONDS)


def _bucket_key(minute_ts: int) -> str:
    return f'load_tracker:minute:{minute_ts}'


def record_request() -> None:
    """Зафиксировать один запрос в middleware."""
    key = _bucket_key(_current_minute_ts())
    try:
        cache.incr(key)
    except ValueError:
        # Ключ ещё не существует — создаём.
        cache.set(key, 1, timeout=CACHE_TTL)


def get_requests_per_minute() -> int:
    """Количество запросов за текущую минуту."""
    return cache.get(_bucket_key(_current_minute_ts()), 0)


def get_requests_per_hour() -> int:
    """Количество запросов за последний час."""
    current = _current_minute_ts()
    total = 0
    for i in range(HOUR_BUCKETS):
        total += cache.get(_bucket_key(current - i * BUCKET_SECONDS), 0)
    return total


def get_load_stats() -> dict:
    """Сводка по нагрузке + проверка лимита."""
    rpm = get_requests_per_minute()
    rph = get_requests_per_hour()
    is_high = rph >= HIGH_LOAD_THRESHOLD

    if is_high:
        logger.warning(
            f'ВЫСОКАЯ НАГРУЗКА СЕРВИСА: {rph} запросов/час '
            f'(лимит {HIGH_LOAD_THRESHOLD}). Текущая минута: {rpm}.'
        )

    return {
        'requests_per_minute': rpm,
        'requests_per_hour': rph,
        'threshold_per_hour': HIGH_LOAD_THRESHOLD,
        'is_high_load': is_high,
    }
