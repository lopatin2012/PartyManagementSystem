# app_helper/load_tracker.py

"""
Учёт нагрузки по HTTP-запросам: live-слой (этап 0).

Счётчики хранятся в памяти **процесса** (web):

- дают мгновенную картину и работают, даже если таблица метрик ещё не создана;
- после перезапуска начинаются заново;
- при нескольких web-процессах каждый считает только свои запросы.

Персистентные метрики, история и дашборд — `app_helper/metrics.py`
(этап 1); этот модуль остаётся источником live-данных и fallback'ом.
"""

import threading
import time

# Лимит запросов в час (используется в отчётах health-чека).
HIGH_LOAD_THRESHOLD = 100_000

BUCKET_SECONDS = 60  # размер бакета — 1 минута.
HOUR_BUCKETS = 60  # 60 минут.

# Классы трафика: пользовательская нагрузка, боты и служебные опросы
# (health, /scheduler/status/, прогресс НК и т.п.).
TRAFFIC_USER = 'user'
TRAFFIC_BOT = 'bot'
TRAFFIC_MONITORING = 'monitoring'
TRAFFIC_CLASSES = (TRAFFIC_USER, TRAFFIC_BOT, TRAFFIC_MONITORING)

# «Нагрузка от клиентов» — пользователи и боты. Служебные опросы
# собственного UI/мониторинга считаются отдельно, чтобы не раздувать цифру.
CLIENT_TRAFFIC = (TRAFFIC_USER, TRAFFIC_BOT)

# Кольцевой буфер: {минута: {класс: количество}}.
# Живёт в памяти процесса; чистится при записи/чтении.
_lock = threading.Lock()
_buckets: dict[int, dict[str, int]] = {}


def _current_minute_ts() -> int:
    """Timestamp начала текущей минуты."""
    now = int(time.time())
    return now - (now % BUCKET_SECONDS)


def _prune(current: int) -> None:
    """Удаляет бакеты старше окна в 60 минут (вызывать под _lock)."""
    oldest = current - (HOUR_BUCKETS - 1) * BUCKET_SECONDS
    for minute in [m for m in _buckets if m < oldest]:
        _buckets.pop(minute, None)


def _classes(traffic_classes) -> tuple:
    if traffic_classes is None:
        return TRAFFIC_CLASSES
    return tuple(traffic_classes)


def reset() -> None:
    """Сбросить счётчики (тесты, диагностика)."""
    with _lock:
        _buckets.clear()


def record_request(traffic_class: str = TRAFFIC_USER) -> None:
    """Зафиксировать один запрос в текущей минуте."""
    if traffic_class not in TRAFFIC_CLASSES:
        traffic_class = TRAFFIC_USER

    minute = _current_minute_ts()
    with _lock:
        _prune(minute)
        bucket = _buckets.setdefault(minute, {})
        bucket[traffic_class] = bucket.get(traffic_class, 0) + 1


def _sum_buckets(traffic_classes) -> int:
    """Сумма по всем бакетам окна (вызывать под _lock после _prune)."""
    classes = _classes(traffic_classes)
    total = 0
    for bucket in _buckets.values():
        total += sum(
            count for cls, count in bucket.items() if cls in classes
        )
    return total


def get_requests_per_minute(traffic_classes=CLIENT_TRAFFIC) -> int:
    """Количество запросов за текущую минуту."""
    current = _current_minute_ts()
    with _lock:
        _prune(current)
        bucket = _buckets.get(current) or {}
        classes = _classes(traffic_classes)
        return sum(
            count for cls, count in bucket.items() if cls in classes
        )


def get_requests_per_hour(traffic_classes=CLIENT_TRAFFIC) -> int:
    """Количество запросов за последние 60 минут (скользящее окно)."""
    current = _current_minute_ts()
    with _lock:
        _prune(current)
        return _sum_buckets(traffic_classes)


def get_peak_per_minute(traffic_classes=CLIENT_TRAFFIC) -> int:
    """Пиковое количество запросов в минуту за последние 60 минут."""
    current = _current_minute_ts()
    classes = _classes(traffic_classes)
    peak = 0
    with _lock:
        _prune(current)
        for bucket in _buckets.values():
            total = sum(
                count for cls, count in bucket.items() if cls in classes
            )
            if total > peak:
                peak = total
    return peak


def get_traffic_breakdown() -> dict:
    """Разбивка за последние 60 минут по классам трафика."""
    current = _current_minute_ts()
    with _lock:
        _prune(current)
        breakdown = {traffic_class: 0 for traffic_class in TRAFFIC_CLASSES}
        for bucket in _buckets.values():
            for cls, count in bucket.items():
                if cls in breakdown:
                    breakdown[cls] += count
    return breakdown


def has_data() -> bool:
    """Есть ли в этом процессе хоть один учтённый запрос."""
    current = _current_minute_ts()
    with _lock:
        _prune(current)
        return any(sum(bucket.values()) for bucket in _buckets.values())
