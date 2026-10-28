# app_helper/metrics.py

"""
Персистентные метрики HTTP-запросов (этап 1).

Сбор
----
`LoadTrackingMiddleware` измеряет длительность запроса, время и количество
запросов к БД (через `connection.execute_wrapper`) и складывает результат в
минутный аккумулятор процесса (`record_request_metric`). Раз в
`REQUEST_METRICS_FLUSH_SECONDS` (по умолчанию 30 с) аккумулятор пишется в
`app_event.RequestMetric` одним UPSERT'ом с прибавлением
(`ON CONFLICT DO UPDATE`): несколько процессов и рестарты складываются,
теряется максимум один интервал.

Чтение
------
- `get_window_stats(minutes)` — сводка за окно: суммы по классам трафика,
  ошибки, p95 по гистограмме, среднее время, пик запросов в минуту.
- `get_series(range_key)` / `get_top_groups(range_key)` — данные дашборда.
- `get_load_stats()` — сводка для health-чека и `/helper/api/v1/status_service/`
  (ключи совместимы с прежней in-process версией).

Если таблицы нет (миграции в проекте gitignored) или БД недоступна, метрики
мягко отключаются, а нагрузка считается по live-счётчикам
`app_helper/load_tracker.py` — страницы и health не падают.
"""

import atexit
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import OperationalError, ProgrammingError, connection, transaction
from django.db.models import Count, Max, Sum
from django.db.models.functions import TruncHour, TruncMinute
from django.utils import timezone

from app_event.models import ExternalCallMetric, RequestMetric, SystemMetric
from app_helper import load_tracker, system_metrics

logger = logging.getLogger(__name__)

# Порог «высокой нагрузки» по количеству запросов в час.
HIGH_LOAD_THRESHOLD = 50_000

# Запрос дольше этого считается медленным.
SLOW_REQUEST_MS = 1000

# Минимум запросов, чтобы доля ошибок считалась осмысленной.
MIN_REQUESTS_FOR_ERROR_RATE = 20

# Предохранитель: сколько строк аккумулятора держать, если БД долго недоступна.
MAX_PENDING_ROWS = 5000

# Границы гистограммы времени ответа (мс).
HISTOGRAM_BOUNDS = {
    'h_lt50': 50,
    'h_lt100': 100,
    'h_lt250': 250,
    'h_lt500': 500,
    'h_lt1000': 1000,
    'h_lt2000': 2000,
    'h_lt5000': 5000,
    'h_gte5000': 5000,
}
HISTOGRAM_FIELDS = tuple(HISTOGRAM_BOUNDS)

COLUMNS = (
    'bucket_start', 'group', 'method', 'status_class', 'traffic_class',
    'count', 'errors', 'slow_count', 'total_ms', 'max_ms',
    'db_ms', 'db_queries', 'peak_inflight',
    *HISTOGRAM_FIELDS,
)
KEY_COLUMNS = (
    'bucket_start', 'group', 'method', 'status_class', 'traffic_class',
)

# Исходящие вызовы: отдельная таблица и ключ.
EXTERNAL_COLUMNS = (
    'bucket_start', 'service', 'method', 'outcome',
    'count', 'errors', 'total_ms', 'max_ms',
    *HISTOGRAM_FIELDS,
)
EXTERNAL_KEY_COLUMNS = ('bucket_start', 'service', 'method', 'outcome')

# Метрики инфраструктуры (CPU/RAM/Postgres).
SYSTEM_COLUMNS = ('bucket_start', 'metric', 'kind', 'value', 'max_value')
SYSTEM_KEY_COLUMNS = ('bucket_start', 'metric', 'kind')

SYSTEM_UNITS = {
    'cpu_percent': '%',
    'mem_percent': '%',
    'load1': '',
    'load5': '',
    'web_rss_mb': 'МБ',
    'db_active_connections': '',
    'db_idle_in_tx': '',
    'db_connections': '',
    'db_max_connections': '',
    'db_size_mb': 'МБ',
    'db_xact_commit': '/мин',
    'db_xact_rollback': '/мин',
    'db_blks_read': '/мин',
    'db_temp_bytes': 'МБ/мин',
}

RANGE_CONFIG = {
    '1h': {'minutes': 60, 'resolution': 'minute', 'label': '1 час'},
    '24h': {'minutes': 24 * 60, 'resolution': 'hour', 'label': '24 часа'},
    '7d': {'minutes': 7 * 24 * 60, 'resolution': 'hour', 'label': '7 дней'},
}
DEFAULT_RANGE = '1h'

_lock = threading.Lock()
_rows: dict[tuple, dict] = {}
_external_rows: dict[tuple, dict] = {}
_system_rows: dict[tuple, dict] = {}
_inflight = 0
_inflight_peaks: dict = {}
_last_flush = time.monotonic()
_disabled = False


# ==========================================
# Настройки (с безопасными значениями по умолчанию).
# ==========================================

def _flush_interval() -> float:
    try:
        value = int(getattr(settings, 'REQUEST_METRICS_FLUSH_SECONDS', 30))
    except (TypeError, ValueError):
        value = 30
    return max(1, value)


def p95_threshold_ms() -> int:
    try:
        return int(getattr(settings, 'LOAD_P95_THRESHOLD_MS', 1500))
    except (TypeError, ValueError):
        return 1500


def error_rate_threshold() -> float:
    try:
        return float(getattr(settings, 'LOAD_ERROR_RATE_THRESHOLD', 0.05))
    except (TypeError, ValueError):
        return 0.05


def retention_days() -> int:
    try:
        return max(1, int(getattr(settings, 'REQUEST_METRICS_RETENTION_DAYS', 14)))
    except (TypeError, ValueError):
        return 14


# ==========================================
# Аккумулятор.
# ==========================================

def _minute_start(moment=None):
    moment = moment or timezone.now()
    return moment.replace(second=0, microsecond=0)


def _hour_start(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def _empty_totals() -> dict:
    totals = {
        'count': 0,
        'errors': 0,
        'slow_count': 0,
        'total_ms': 0,
        'max_ms': 0,
        'db_ms': 0,
        'db_queries': 0,
    }
    totals.update({field: 0 for field in HISTOGRAM_FIELDS})
    return totals


def _add_totals(target: dict, source: dict) -> None:
    for field in (
        'count', 'errors', 'slow_count', 'total_ms',
        'db_ms', 'db_queries',
    ):
        target[field] += source.get(field) or 0
    if (source.get('max_ms') or 0) > target['max_ms']:
        target['max_ms'] = source.get('max_ms') or 0
    for field in HISTOGRAM_FIELDS:
        target[field] += source.get(field) or 0


def _histogram_field(duration_ms: int) -> str:
    for field in HISTOGRAM_FIELDS[:-1]:
        if duration_ms < HISTOGRAM_BOUNDS[field]:
            return field
    return HISTOGRAM_FIELDS[-1]


def reset() -> None:
    """Сбросить аккумулятор и отключение (тесты/диагностика)."""
    global _inflight, _disabled, _last_flush
    with _lock:
        _rows.clear()
        _external_rows.clear()
        _system_rows.clear()
        _inflight_peaks.clear()
        _inflight = 0
        _disabled = False
        _last_flush = time.monotonic()


def start_request() -> None:
    """Отметить начало запроса (для пика параллельных)."""
    global _inflight
    minute = _minute_start()
    with _lock:
        _inflight += 1
        if _inflight > _inflight_peaks.get(minute, 0):
            _inflight_peaks[minute] = _inflight


def finish_request() -> None:
    """Отметить завершение запроса."""
    global _inflight
    with _lock:
        if _inflight > 0:
            _inflight -= 1


def record_request_metric(
        *, group: str, method: str, status_class: str, traffic_class: str,
        duration_ms: float, db_ms: float = 0.0, db_queries: int = 0) -> None:
    """Добавить один запрос в минутный аккумулятор."""
    if _disabled:
        return

    minute = _minute_start()
    group = (group or 'other')[:100]
    method = (method or 'GET')[:10]
    duration = max(0, int(round(duration_ms)))
    db_time = max(0, int(round(db_ms)))
    histogram = _histogram_field(duration)
    key = (minute, group, method, status_class, traffic_class)

    with _lock:
        row = _rows.get(key)
        if row is None:
            if len(_rows) >= MAX_PENDING_ROWS:
                # БД долго недоступна — не растим память бесконечно.
                return
            row = {
                'bucket_start': minute,
                'group': group,
                'method': method,
                'status_class': status_class,
                'traffic_class': traffic_class,
                'count': 0,
                'errors': 0,
                'slow_count': 0,
                'total_ms': 0,
                'max_ms': 0,
                'db_ms': 0,
                'db_queries': 0,
                'peak_inflight': 0,
                **{field: 0 for field in HISTOGRAM_FIELDS},
            }
            _rows[key] = row

        row['count'] += 1
        if status_class == '5xx':
            row['errors'] += 1
        if duration >= SLOW_REQUEST_MS:
            row['slow_count'] += 1
        row['total_ms'] += duration
        if duration > row['max_ms']:
            row['max_ms'] = duration
        row['db_ms'] += db_time
        row['db_queries'] += max(0, int(db_queries))
        row[histogram] += 1

    _maybe_flush()


def pending_count() -> int:
    """Сколько записей ждёт записи в БД (диагностика/тесты)."""
    with _lock:
        return len(_rows)


def pending_external_count() -> int:
    """Сколько записей о внешних вызовах ждёт записи в БД."""
    with _lock:
        return len(_external_rows)


def record_external_call(*, service: str, method: str, outcome: str,
                         duration_ms: float) -> None:
    """Добавить один исходящий HTTP-вызов в минутный аккумулятор."""
    if _disabled:
        return

    minute = _minute_start()
    service = (service or 'other')[:30]
    method = (method or 'GET')[:10].upper()
    outcome = (outcome or 'ok')[:10]
    duration = max(0, int(round(duration_ms)))
    histogram = _histogram_field(duration)
    key = (minute, service, method, outcome)

    with _lock:
        row = _external_rows.get(key)
        if row is None:
            if len(_external_rows) >= MAX_PENDING_ROWS:
                return
            row = {
                'bucket_start': minute,
                'service': service,
                'method': method,
                'outcome': outcome,
                'count': 0,
                'errors': 0,
                'total_ms': 0,
                'max_ms': 0,
                **{field: 0 for field in HISTOGRAM_FIELDS},
            }
            _external_rows[key] = row

        row['count'] += 1
        if outcome != 'ok':
            row['errors'] += 1
        row['total_ms'] += duration
        if duration > row['max_ms']:
            row['max_ms'] = duration
        row[histogram] += 1

    _maybe_flush()


def _merge_external_row(item: dict) -> None:
    """Вернуть несохранённый внешний вызов в аккумулятор (под _lock)."""
    key = tuple(item[column] for column in EXTERNAL_KEY_COLUMNS)
    target = _external_rows.get(key)
    if target is None:
        _external_rows[key] = dict(item)
        return
    for field in ('count', 'errors', 'total_ms'):
        target[field] += item.get(field) or 0
    target['max_ms'] = max(target['max_ms'], item.get('max_ms') or 0)
    for field in HISTOGRAM_FIELDS:
        target[field] += item.get(field) or 0


def _merge_row(item: dict) -> None:
    """Вернуть несохранённую строку в аккумулятор (вызывать под _lock)."""
    key = tuple(item[column] for column in KEY_COLUMNS)
    target = _rows.get(key)
    if target is None:
        _rows[key] = dict(item)
        return
    for field in (
        'count', 'errors', 'slow_count', 'total_ms',
        'db_ms', 'db_queries',
    ):
        target[field] += item.get(field) or 0
    target['max_ms'] = max(target['max_ms'], item.get('max_ms') or 0)
    target['peak_inflight'] = max(
        target.get('peak_inflight', 0), item.get('peak_inflight') or 0,
    )
    for field in HISTOGRAM_FIELDS:
        target[field] += item.get(field) or 0


def _maybe_flush() -> None:
    """Пишем в БД не чаще, чем раз в `REQUEST_METRICS_FLUSH_SECONDS`."""
    global _last_flush
    with _lock:
        if time.monotonic() - _last_flush < _flush_interval():
            return
        _last_flush = time.monotonic()
    flush_metrics()


def _apply_system_samples(samples: dict) -> None:
    """Добавить сэмплы инфраструктуры в минутный аккумулятор (под _lock)."""
    minute = _minute_start()
    for metric, payload in samples.items():
        kind, value = payload
        key = (minute, metric, kind)
        row = _system_rows.get(key)
        if row is None:
            row = {
                'bucket_start': minute,
                'metric': metric,
                'kind': kind,
                'value': 0.0,
                'max_value': 0.0,
            }
            _system_rows[key] = row
        if kind == 'counter':
            row['value'] += value
        elif value > row['value']:
            row['value'] = value
        if value > row['max_value']:
            row['max_value'] = value


def _merge_system_row(item: dict) -> None:
    """Вернуть несохранённый сэмпл в аккумулятор (под _lock)."""
    key = tuple(item[column] for column in SYSTEM_KEY_COLUMNS)
    target = _system_rows.get(key)
    if target is None:
        _system_rows[key] = dict(item)
        return
    if item['kind'] == 'counter':
        target['value'] += item['value']
    else:
        target['value'] = max(target['value'], item['value'])
    target['max_value'] = max(target['max_value'], item['max_value'])


def flush_metrics() -> int:
    """Записать аккумуляторы в БД. Возвращает число строк запросов."""
    global _disabled, _inflight_peaks

    samples = system_metrics.collect_samples()

    with _lock:
        _apply_system_samples(samples)
        if not _rows and not _external_rows and not _system_rows:
            return 0
        snapshot = list(_rows.values())
        _rows.clear()
        external_snapshot = list(_external_rows.values())
        _external_rows.clear()
        system_snapshot = list(_system_rows.values())
        _system_rows.clear()
        peaks = dict(_inflight_peaks)
        cutoff = _minute_start() - timedelta(hours=1)
        _inflight_peaks = {
            minute: peak
            for minute, peak in _inflight_peaks.items()
            if minute >= cutoff
        }

    rows = []
    for row in snapshot:
        item = dict(row)
        item['peak_inflight'] = max(
            item.get('peak_inflight') or 0,
            peaks.get(item['bucket_start'], 0),
        )
        rows.append(item)
    external_rows = [dict(row) for row in external_snapshot]
    system_rows = [dict(row) for row in system_snapshot]

    try:
        with transaction.atomic():
            _write_rows(rows)
            _write_external_rows(external_rows)
            _write_system_rows(system_rows)
    except Exception as exc:  # noqa: BLE001 — метрики не должны ломать запрос
        logger.exception(f'Не удалось сохранить метрики запросов: {exc}')
        with _lock:
            for item in rows:
                _merge_row(item)
            for item in external_rows:
                _merge_external_row(item)
            for item in system_rows:
                _merge_system_row(item)
        _disable_on_error(exc)
        return 0

    return len(rows)


def _is_missing_table(exc) -> bool:
    text = str(exc).lower()
    return 'does not exist' in text or 'undefinedtable' in text


def _disable_on_error(exc) -> None:
    """Отключить метрики, если таблицы нет; иначе — просто предупредить."""
    global _disabled
    if _is_missing_table(exc):
        if not _disabled:
            logger.warning(
                'Таблица app_event_requestmetric недоступна — метрики '
                'запросов отключены, нагрузка считается по live-счётчикам. '
                'Примените миграции.'
            )
        _disabled = True
        with _lock:
            _rows.clear()
            _external_rows.clear()
            _system_rows.clear()
        return
    logger.warning(f'Метрики запросов временно недоступны: {exc}')


def _quoted_columns() -> str:
    return ', '.join(
        f'"{column}"' if column == 'group' else column
        for column in COLUMNS
    )


def _upsert_sql() -> str:
    placeholders = ', '.join(['%s'] * len(COLUMNS))
    updates = []
    for column in COLUMNS:
        if column in KEY_COLUMNS:
            continue
        if column in ('max_ms', 'peak_inflight'):
            updates.append(
                f'{column} = GREATEST('
                f'app_event_requestmetric.{column}, EXCLUDED.{column})'
            )
        else:
            updates.append(
                f'{column} = app_event_requestmetric.{column} '
                f'+ EXCLUDED.{column}'
            )
    return (
        f'INSERT INTO app_event_requestmetric ({_quoted_columns()}) '
        f'VALUES ({placeholders}) '
        f'ON CONFLICT (bucket_start, "group", method, status_class, '
        f'traffic_class) DO UPDATE SET {", ".join(updates)}'
    )


def _write_rows(rows: list) -> None:
    """UPSERT пачки строк одним executemany."""
    if not rows:
        return
    sql = _upsert_sql()
    params = [tuple(item[column] for column in COLUMNS) for item in rows]
    with connection.cursor() as cursor:
        cursor.executemany(sql, params)


def _external_upsert_sql() -> str:
    placeholders = ', '.join(['%s'] * len(EXTERNAL_COLUMNS))
    updates = []
    for column in EXTERNAL_COLUMNS:
        if column in EXTERNAL_KEY_COLUMNS:
            continue
        if column == 'max_ms':
            updates.append(
                'max_ms = GREATEST('
                'app_event_externalcallmetric.max_ms, EXCLUDED.max_ms)'
            )
        else:
            updates.append(
                f'{column} = app_event_externalcallmetric.{column} '
                f'+ EXCLUDED.{column}'
            )
    columns = ', '.join(EXTERNAL_COLUMNS)
    return (
        f'INSERT INTO app_event_externalcallmetric ({columns}) '
        f'VALUES ({placeholders}) '
        f'ON CONFLICT (bucket_start, service, method, outcome) '
        f'DO UPDATE SET {", ".join(updates)}'
    )


def _write_external_rows(rows: list) -> None:
    """UPSERT пачки внешних вызовов одним executemany."""
    if not rows:
        return
    sql = _external_upsert_sql()
    params = [
        tuple(item[column] for column in EXTERNAL_COLUMNS) for item in rows
    ]
    with connection.cursor() as cursor:
        cursor.executemany(sql, params)


def _system_upsert_sql() -> str:
    placeholders = ', '.join(['%s'] * len(SYSTEM_COLUMNS))
    columns = ', '.join(SYSTEM_COLUMNS)
    return (
        f'INSERT INTO app_event_systemmetric ({columns}) '
        f'VALUES ({placeholders}) '
        f'ON CONFLICT (bucket_start, metric, kind) DO UPDATE SET '
        f'value = CASE WHEN EXCLUDED.kind = \'counter\' '
        f'THEN app_event_systemmetric.value + EXCLUDED.value '
        f'ELSE GREATEST(app_event_systemmetric.value, EXCLUDED.value) END, '
        f'max_value = GREATEST('
        f'app_event_systemmetric.max_value, EXCLUDED.max_value)'
    )


def _write_system_rows(rows: list) -> None:
    """UPSERT пачки сэмплов инфраструктуры."""
    if not rows:
        return
    sql = _system_upsert_sql()
    params = [
        tuple(item[column] for column in SYSTEM_COLUMNS) for item in rows
    ]
    with connection.cursor() as cursor:
        cursor.executemany(sql, params)


# ==========================================
# Чтение и агрегация.
# ==========================================

def _p95_ms(hist: dict, count: int, max_ms: int) -> int:
    """p95 по гистограмме (приблизительно, по верхним границам бакетов)."""
    if count <= 0:
        return 0
    threshold = count * 0.95
    cumulative = 0
    for field in HISTOGRAM_FIELDS:
        cumulative += hist.get(field) or 0
        if cumulative >= threshold:
            if field == HISTOGRAM_FIELDS[-1]:
                return max(max_ms or 0, HISTOGRAM_BOUNDS[field])
            return HISTOGRAM_BOUNDS[field]
    return max_ms or 0


def _safe_per_class(start):
    """Суммы по классам трафика за окно (None — БД недоступна)."""
    if _disabled:
        return None
    try:
        return list(
            RequestMetric.objects
            .filter(bucket_start__gte=start)
            .values('traffic_class')
            .annotate(
                count=Sum('count'),
                errors=Sum('errors'),
                slow_count=Sum('slow_count'),
                total_ms=Sum('total_ms'),
                max_ms=Max('max_ms'),
                db_ms=Sum('db_ms'),
                db_queries=Sum('db_queries'),
                **{field: Sum(field) for field in HISTOGRAM_FIELDS},
            )
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def _safe_per_minute(start):
    """Суммы по минутам для клиентского трафика."""
    if _disabled:
        return None
    try:
        return list(
            RequestMetric.objects
            .filter(
                bucket_start__gte=start,
                traffic_class__in=load_tracker.CLIENT_TRAFFIC,
            )
            .values('bucket_start')
            .annotate(count=Sum('count'))
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def _memory_stats(minutes: int, source: str) -> dict:
    """Fallback без БД: live-счётчики процесса (без p95 и ошибок)."""
    breakdown = load_tracker.get_traffic_breakdown()
    total = sum(breakdown.values())
    return {
        'minutes': minutes,
        'start': None,
        'end': timezone.now(),
        'total': total,
        'user': breakdown[load_tracker.TRAFFIC_USER],
        'bot': breakdown[load_tracker.TRAFFIC_BOT],
        'monitoring': breakdown[load_tracker.TRAFFIC_MONITORING],
        'errors': 0,
        'error_rate': 0.0,
        'p95_ms': 0,
        'avg_ms': 0,
        'max_ms': 0,
        'avg_db_ms': 0.0,
        'avg_db_queries': 0.0,
        'slow_count': 0,
        'peak_per_minute': load_tracker.get_peak_per_minute(),
        'requests_per_minute': load_tracker.get_requests_per_minute(),
        'has_data': total > 0,
        'source': source,
    }


def get_window_stats(minutes: int = 60) -> dict:
    """Сводка за последние `minutes` минут (БД + незаписанный аккумулятор)."""
    minutes = max(1, int(minutes))
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=minutes))

    aggregates = _safe_per_class(start)
    if aggregates is None:
        return _memory_stats(minutes, 'memory')

    totals = {cls: _empty_totals() for cls in load_tracker.TRAFFIC_CLASSES}
    for row in aggregates:
        cls = row.get('traffic_class') or load_tracker.TRAFFIC_USER
        _add_totals(totals.setdefault(cls, _empty_totals()), row)

    with _lock:
        pending = [
            dict(row) for row in _rows.values()
            if row['bucket_start'] >= start
        ]
    for row in pending:
        _add_totals(
            totals.setdefault(row['traffic_class'], _empty_totals()), row,
        )

    user = totals[load_tracker.TRAFFIC_USER]
    bot = totals[load_tracker.TRAFFIC_BOT]
    monitoring = totals[load_tracker.TRAFFIC_MONITORING]
    client = _empty_totals()
    _add_totals(client, user)
    _add_totals(client, bot)

    per_minute = {}
    for row in (_safe_per_minute(start) or []):
        per_minute[row['bucket_start']] = row.get('count') or 0
    for row in pending:
        if row['traffic_class'] in load_tracker.CLIENT_TRAFFIC:
            minute = row['bucket_start']
            per_minute[minute] = per_minute.get(minute, 0) + row['count']

    client_count = client['count']
    p95 = _p95_ms(
        {field: client[field] for field in HISTOGRAM_FIELDS},
        client_count,
        client['max_ms'],
    )

    return {
        'minutes': minutes,
        'start': start,
        'end': end,
        'total': user['count'] + bot['count'] + monitoring['count'],
        'user': user['count'],
        'bot': bot['count'],
        'monitoring': monitoring['count'],
        'errors': client['errors'],
        'error_rate': (
            client['errors'] / client_count if client_count else 0.0
        ),
        'p95_ms': p95,
        'avg_ms': round(client['total_ms'] / client_count, 1) if client_count else 0,
        'max_ms': client['max_ms'],
        'avg_db_ms': (
            round(client['db_ms'] / client_count, 1) if client_count else 0
        ),
        'avg_db_queries': (
            round(client['db_queries'] / client_count, 2)
            if client_count else 0
        ),
        'slow_count': client['slow_count'],
        'peak_per_minute': max(per_minute.values(), default=0),
        'requests_per_minute': per_minute.get(_minute_start(end), 0),
        'has_data': (user['count'] + bot['count'] + monitoring['count']) > 0,
        'source': 'db',
    }


def get_load_stats() -> dict:
    """Сводка нагрузки для health-чека и status API."""
    stats = get_window_stats(60)
    requests_per_hour = stats['user'] + stats['bot']
    has_data = stats['has_data']
    p95 = stats['p95_ms']
    error_rate = stats['error_rate']
    peak = stats['peak_per_minute']

    reasons = []
    if has_data:
        if requests_per_hour >= HIGH_LOAD_THRESHOLD:
            reasons.append(
                f'{requests_per_hour} запросов/час '
                f'(порог {HIGH_LOAD_THRESHOLD})'
            )
        if p95 > p95_threshold_ms():
            reasons.append(f'p95 {p95} мс (порог {p95_threshold_ms()})')
        if (
            stats['user'] + stats['bot'] >= MIN_REQUESTS_FOR_ERROR_RATE
            and error_rate > error_rate_threshold()
        ):
            reasons.append(
                f'ошибок {error_rate:.1%} '
                f'(порог {error_rate_threshold():.1%})'
            )
    is_high = bool(reasons)

    if not has_data:
        message = (
            'Нагрузка: нет данных — запросы ещё не записаны в метрики '
            '(или таблица метрик недоступна)'
        )
    elif is_high:
        message = 'Высокая нагрузка: ' + '; '.join(reasons)
        logger.warning(f'ВЫСОКАЯ НАГРУЗКА СЕРВИСА: {message}')
    else:
        message = (
            f'Нагрузка в норме: {requests_per_hour} запросов за 60 мин, '
            f'пик {peak}/мин, p95 {p95} мс, '
            f'ошибок {error_rate:.1%}'
        )

    return {
        'requests_per_minute': stats['requests_per_minute'],
        'requests_per_hour': requests_per_hour,
        'requests_hour_monitoring': stats['monitoring'],
        'requests_hour_bot': stats['bot'],
        'requests_peak_per_minute': peak,
        'p95_ms': p95,
        'error_rate': round(error_rate, 4),
        'slow_count': stats['slow_count'],
        'avg_db_ms': stats['avg_db_ms'],
        'threshold_per_hour': HIGH_LOAD_THRESHOLD,
        'p95_threshold_ms': p95_threshold_ms(),
        'error_rate_threshold': error_rate_threshold(),
        'is_high_load': is_high,
        'has_data': has_data,
        'source': stats['source'],
        'message': message,
    }


def get_client_totals(minutes: int = 60) -> dict:
    """Короткая сводка для футера: пользовательские запросы, пик, служебные."""
    stats = get_window_stats(minutes)
    return {
        'requests': stats['user'] + stats['bot'],
        'peak': stats['peak_per_minute'],
        'monitoring': stats['monitoring'],
        'has_data': stats['has_data'],
        'source': stats['source'],
    }


# ==========================================
# Ряды и топы для дашборда.
# ==========================================

def _range_settings(range_key: str) -> dict:
    key = (range_key or DEFAULT_RANGE).lower()
    if key not in RANGE_CONFIG:
        key = DEFAULT_RANGE
    return key, RANGE_CONFIG[key]


def _safe_series(start, trunc):
    if _disabled:
        return None
    try:
        return list(
            RequestMetric.objects
            .filter(bucket_start__gte=start)
            .annotate(bucket=trunc('bucket_start'))
            .values('bucket', 'traffic_class')
            .annotate(
                count=Sum('count'),
                errors=Sum('errors'),
                slow_count=Sum('slow_count'),
                total_ms=Sum('total_ms'),
                max_ms=Max('max_ms'),
                **{field: Sum(field) for field in HISTOGRAM_FIELDS},
            )
            .order_by('bucket')
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def get_series(range_key: str = DEFAULT_RANGE) -> dict:
    """Ряд точек для графика (нули для пустых бакетов)."""
    key, cfg = _range_settings(range_key)
    minutes = cfg['minutes']
    resolution = cfg['resolution']
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=minutes))

    if resolution == 'hour':
        start = _hour_start(start)
        trunc_db = TruncHour
        trunc_py = _hour_start
        step = timedelta(hours=1)
    else:
        trunc_db = TruncMinute
        trunc_py = _minute_start
        step = timedelta(minutes=1)

    rows = _safe_series(start, trunc_db)
    source = 'db'
    if rows is None:
        rows = []
        source = 'memory'

    per_bucket: dict = {}

    def _point(bucket):
        point = per_bucket.get(bucket)
        if point is None:
            point = {
                'user': 0, 'bot': 0, 'monitoring': 0,
                'errors': 0, 'slow_count': 0, 'total_ms': 0,
                'max_ms': 0, 'hist': {f: 0 for f in HISTOGRAM_FIELDS},
            }
            per_bucket[bucket] = point
        return point

    for row in rows:
        point = _point(row['bucket'])
        cls = row.get('traffic_class') or load_tracker.TRAFFIC_USER
        if cls in point:
            point[cls] += row.get('count') or 0
        point['errors'] += row.get('errors') or 0
        point['slow_count'] += row.get('slow_count') or 0
        point['total_ms'] += row.get('total_ms') or 0
        point['max_ms'] = max(point['max_ms'], row.get('max_ms') or 0)
        for field in HISTOGRAM_FIELDS:
            point['hist'][field] += row.get(field) or 0

    with _lock:
        pending = [
            dict(row) for row in _rows.values()
            if row['bucket_start'] >= start
        ]
    for row in pending:
        point = _point(trunc_py(row['bucket_start']))
        cls = row['traffic_class']
        if cls in point:
            point[cls] += row['count']
        point['errors'] += row['errors']
        point['slow_count'] += row['slow_count']
        point['total_ms'] += row['total_ms']
        point['max_ms'] = max(point['max_ms'], row['max_ms'])
        for field in HISTOGRAM_FIELDS:
            point['hist'][field] += row[field]

    points = []
    bucket = trunc_py(start)
    last = trunc_py(end)
    while bucket <= last:
        point = per_bucket.get(bucket)
        if point is None:
            point = {
                'user': 0, 'bot': 0, 'monitoring': 0,
                'errors': 0, 'slow_count': 0, 'total_ms': 0,
                'max_ms': 0, 'hist': {f: 0 for f in HISTOGRAM_FIELDS},
            }
        client_count = point['user'] + point['bot']
        points.append({
            'bucket': bucket.isoformat(),
            'user': point['user'],
            'bot': point['bot'],
            'monitoring': point['monitoring'],
            'total': point['user'] + point['bot'] + point['monitoring'],
            'errors': point['errors'],
            'slow_count': point['slow_count'],
            'avg_ms': (
                round(point['total_ms'] / client_count, 1)
                if client_count else 0
            ),
            'p95_ms': _p95_ms(point['hist'], client_count, point['max_ms']),
        })
        bucket += step

    return {
        'range': key,
        'label': cfg['label'],
        'resolution': resolution,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'source': source,
        'points': points,
    }


def _safe_group_stats(start):
    if _disabled:
        return None
    try:
        return list(
            RequestMetric.objects
            .filter(
                bucket_start__gte=start,
                traffic_class__in=load_tracker.CLIENT_TRAFFIC,
            )
            .values('group')
            .annotate(
                count=Sum('count'),
                errors=Sum('errors'),
                slow_count=Sum('slow_count'),
                total_ms=Sum('total_ms'),
                max_ms=Max('max_ms'),
                **{field: Sum(field) for field in HISTOGRAM_FIELDS},
            )
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def get_top_groups(range_key: str = DEFAULT_RANGE, limit: int = 10) -> dict:
    """Топ-endpoint'ов: по количеству и по p95."""
    _, cfg = _range_settings(range_key)
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=cfg['minutes']))

    rows = _safe_group_stats(start)
    if rows is None:
        rows = []

    by_group = {}
    for row in rows:
        target = by_group.setdefault(row['group'], _empty_totals())
        _add_totals(target, row)

    with _lock:
        pending = [
            dict(row) for row in _rows.values()
            if row['bucket_start'] >= start
            and row['traffic_class'] in load_tracker.CLIENT_TRAFFIC
        ]
    for row in pending:
        target = by_group.setdefault(row['group'], _empty_totals())
        _add_totals(target, row)

    groups = []
    for group, totals in by_group.items():
        count = totals['count']
        groups.append({
            'group': group,
            'count': count,
            'errors': totals['errors'],
            'error_rate': (totals['errors'] / count) if count else 0.0,
            'slow_count': totals['slow_count'],
            'avg_ms': round(totals['total_ms'] / count, 1) if count else 0,
            'max_ms': totals['max_ms'],
            'p95_ms': _p95_ms(
                {field: totals[field] for field in HISTOGRAM_FIELDS},
                count,
                totals['max_ms'],
            ),
        })

    by_count = sorted(groups, key=lambda item: item['count'], reverse=True)
    by_p95 = sorted(
        (item for item in groups if item['count'] >= 10),
        key=lambda item: item['p95_ms'],
        reverse=True,
    )
    return {
        'by_count': by_count[:limit],
        'by_p95': by_p95[:limit],
    }


SERVICE_LABELS = {
    'cz': 'Честный Знак (True API)',
    'nk': 'Национальный каталог',
    'suz': 'СУЗ-Облако',
    'sign': 'Сервис подписей',
    'factory': 'Серверы заводов',
    '1c': '1С',
    'cz_rate_limit': 'Ожидание лимита ЧЗ',
    'probe': 'Проверки доступности',
}


def _empty_external() -> dict:
    totals = {'count': 0, 'errors': 0, 'total_ms': 0, 'max_ms': 0}
    totals.update({field: 0 for field in HISTOGRAM_FIELDS})
    return totals


def _add_external(target: dict, source: dict) -> None:
    for field in ('count', 'errors', 'total_ms'):
        target[field] += source.get(field) or 0
    target['max_ms'] = max(target['max_ms'], source.get('max_ms') or 0)
    for field in HISTOGRAM_FIELDS:
        target[field] += source.get(field) or 0


def _safe_external(start):
    if _disabled:
        return None
    try:
        return list(
            ExternalCallMetric.objects
            .filter(bucket_start__gte=start)
            .values('service')
            .annotate(
                count=Sum('count'),
                errors=Sum('errors'),
                total_ms=Sum('total_ms'),
                max_ms=Max('max_ms'),
                **{field: Sum(field) for field in HISTOGRAM_FIELDS},
            )
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def get_external_stats(minutes: int = 60) -> dict:
    """Сводка исходящих HTTP-вызовов по сервисам за окно."""
    minutes = max(1, int(minutes))
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=minutes))

    rows = _safe_external(start)
    source = 'db'
    if rows is None:
        rows = []
        source = 'memory'

    by_service: dict = {}
    for row in rows:
        _add_external(
            by_service.setdefault(row['service'], _empty_external()), row,
        )

    with _lock:
        pending = [
            dict(row) for row in _external_rows.values()
            if row['bucket_start'] >= start
        ]
    for row in pending:
        _add_external(
            by_service.setdefault(row['service'], _empty_external()), row,
        )

    services = []
    for service, totals in by_service.items():
        count = totals['count']
        services.append({
            'service': service,
            'label': SERVICE_LABELS.get(service, service),
            'count': count,
            'errors': totals['errors'],
            'error_rate': (totals['errors'] / count) if count else 0.0,
            'avg_ms': round(totals['total_ms'] / count, 1) if count else 0,
            'max_ms': totals['max_ms'],
            'p95_ms': _p95_ms(
                {field: totals[field] for field in HISTOGRAM_FIELDS},
                count,
                totals['max_ms'],
            ),
        })
    services.sort(key=lambda item: item['count'], reverse=True)

    return {
        'minutes': minutes,
        'start': start,
        'end': end,
        'services': services,
        'total': sum(item['count'] for item in services),
        'errors': sum(item['errors'] for item in services),
        'has_data': bool(services),
        'source': source,
    }


def external_error_threshold() -> float:
    try:
        return float(getattr(settings, 'EXTERNAL_ERROR_RATE_THRESHOLD', 0.2))
    except (TypeError, ValueError):
        return 0.2


def external_min_calls() -> int:
    try:
        return max(1, int(getattr(settings, 'EXTERNAL_MIN_CALLS', 5)))
    except (TypeError, ValueError):
        return 5


def external_check(minutes: int = 60) -> dict:
    """Проверка «Внешние API» для health/status: доля ошибок по сервисам."""
    stats = get_external_stats(minutes)
    services = stats['services']
    threshold = external_error_threshold()
    minimum = external_min_calls()

    problems = [
        item for item in services
        # Проверки доступности не считаем бизнес-ошибками: для них есть
        # отдельный чек «Заводы/подписи».
        if item['service'] != 'probe'
        and item['count'] >= minimum
        and item['error_rate'] > threshold
    ]

    if not services:
        message = 'Нет данных о внешних вызовах'
        ok = True
    elif problems:
        ok = False
        message = 'Ошибки внешних API: ' + '; '.join(
            f'{item["label"]} {item["error_rate"]:.1%} '
            f'({item["errors"]}/{item["count"]})'
            for item in problems
        )
    else:
        ok = True
        message = (
            f'Внешние API: {stats["total"]} вызовов, '
            f'ошибок {stats["errors"]}'
        )

    return {
        'name': 'Внешние API',
        'ok': ok,
        'message': message,
        'details': {
            'services': services,
            'problems': [item['service'] for item in problems],
            'threshold': threshold,
            'min_calls': minimum,
            'has_data': stats['has_data'],
            'source': stats['source'],
        },
    }


def queue_max_ready() -> int:
    try:
        return max(1, int(getattr(settings, 'QUEUE_MAX_READY', 20)))
    except (TypeError, ValueError):
        return 20


def queue_max_ready_age_seconds() -> int:
    try:
        return max(30, int(getattr(settings, 'QUEUE_MAX_READY_AGE_SECONDS', 600)))
    except (TypeError, ValueError):
        return 600


def queue_max_failed_per_hour() -> int:
    try:
        return max(1, int(getattr(settings, 'QUEUE_MAX_FAILED_PER_HOUR', 5)))
    except (TypeError, ValueError):
        return 5


def get_queue_stats(hours: int = 1) -> dict:
    """Состояние очереди django-tasks-db и длительности задач за `hours`."""
    from django_tasks.base import TaskResultStatus
    from django_tasks_db.models import DBTaskResult

    now = timezone.now()
    since = now - timedelta(hours=max(1, int(hours)))
    try:
        by_status = {
            str(row['status']): row['count']
            for row in DBTaskResult.objects
            .values('status')
            .annotate(count=Count('id'))
        }
        ready_qs = DBTaskResult.objects.filter(status=TaskResultStatus.READY)
        oldest = (
            ready_qs.order_by('enqueued_at')
            .values_list('enqueued_at', flat=True)
            .first()
        )
        finished_qs = DBTaskResult.objects.filter(
            status__in=[
                TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED,
            ],
            finished_at__gte=since,
        )
        durations = []
        for started, finished in finished_qs.values_list(
                'started_at', 'finished_at')[:1000]:
            if started and finished:
                durations.append((finished - started).total_seconds() * 1000)
        failures = DBTaskResult.objects.filter(
            status=TaskResultStatus.FAILED,
            finished_at__gte=since,
        ).count()
    except (ProgrammingError, OperationalError) as exc:
        logger.warning(f'Статистика очереди недоступна: {exc}')
        return {
            'available': False,
            'ready': 0,
            'running': 0,
            'oldest_ready_age_seconds': 0,
            'finished_hour': 0,
            'failed_hour': 0,
            'p95_duration_ms': 0,
            'max_duration_ms': 0,
            'has_data': False,
        }

    durations.sort()
    p95 = 0
    if durations:
        index = min(len(durations) - 1, int(len(durations) * 0.95))
        p95 = round(durations[index], 1)

    return {
        'available': True,
        'ready': int(by_status.get(str(TaskResultStatus.READY), 0)),
        'running': int(by_status.get(str(TaskResultStatus.RUNNING), 0)),
        'successful': int(by_status.get(str(TaskResultStatus.SUCCESSFUL), 0)),
        'failed': int(by_status.get(str(TaskResultStatus.FAILED), 0)),
        'oldest_ready_age_seconds': (
            int((now - oldest).total_seconds()) if oldest else 0
        ),
        'finished_hour': len(durations),
        'failed_hour': failures,
        'p95_duration_ms': p95,
        'max_duration_ms': round(durations[-1], 1) if durations else 0,
        'has_data': bool(by_status),
    }


def queue_check() -> dict:
    """Проверка «Очередь задач» для health/status."""
    stats = get_queue_stats()
    max_ready = queue_max_ready()
    max_age = queue_max_ready_age_seconds()
    max_failed = queue_max_failed_per_hour()

    if not stats['available']:
        ok = True
        message = 'Очередь задач недоступна (нет таблицы dbtaskresult)'
    elif stats['oldest_ready_age_seconds'] > max_age:
        ok = False
        message = (
            f'Старейшая задача в очереди ждёт '
            f'{stats["oldest_ready_age_seconds"] // 60} мин '
            f'(порог {max_age // 60} мин)'
        )
    elif stats['ready'] > max_ready:
        ok = False
        message = (
            f'В очереди {stats["ready"]} задач '
            f'(порог {max_ready}), выполняется {stats["running"]}'
        )
    elif stats['failed_hour'] > max_failed:
        ok = False
        message = (
            f'Ошибок фоновых задач за час: {stats["failed_hour"]} '
            f'(порог {max_failed})'
        )
    else:
        ok = True
        message = (
            f'Очередь в норме: готовы {stats["ready"]}, '
            f'выполняются {stats["running"]}, '
            f'ошибок за час {stats["failed_hour"]}'
        )

    return {
        'name': 'Очередь задач',
        'ok': ok,
        'message': message,
        'details': {
            **stats,
            'max_ready': max_ready,
            'max_ready_age_seconds': max_age,
            'max_failed_per_hour': max_failed,
        },
    }


def _safe_system(start):
    if _disabled:
        return None
    try:
        return list(
            SystemMetric.objects
            .filter(bucket_start__gte=start)
            .values('bucket_start', 'metric', 'kind', 'value', 'max_value')
            .order_by('bucket_start')
        )
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return None


def get_system_stats(minutes: int = 60) -> dict:
    """Последние значения инфраструктуры за окно."""
    minutes = max(1, int(minutes))
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=minutes))

    rows = _safe_system(start)
    source = 'db'
    if rows is None:
        rows = []
        source = 'memory'

    with _lock:
        pending = [
            {
                'bucket_start': row['bucket_start'],
                'metric': row['metric'],
                'kind': row['kind'],
                'value': row['value'],
                'max_value': row['max_value'],
            }
            for row in _system_rows.values()
            if row['bucket_start'] >= start
        ]

    grouped = {}
    for row in list(rows) + pending:
        item = grouped.setdefault(
            row['metric'],
            {'kind': row['kind'], 'value': 0.0, 'max_value': 0.0},
        )
        if row['kind'] == 'counter':
            item['value'] += row['value']
        else:
            item['value'] = row['value']  # строки по возрастанию минуты
        item['max_value'] = max(item['max_value'], row['max_value'])

    metrics = []
    for name, item in grouped.items():
        value = item['value']
        if name == 'db_temp_bytes':
            value = value / 1024 / 1024
        metrics.append({
            'metric': name,
            'kind': item['kind'],
            'value': round(value, 2),
            'max': round(item['max_value'], 2),
            'unit': SYSTEM_UNITS.get(name, ''),
        })
    metrics.sort(key=lambda item: item['metric'])

    return {
        'minutes': minutes,
        'start': start,
        'end': end,
        'metrics': metrics,
        'has_data': bool(metrics),
        'has_host': any(
            item['metric'] in ('cpu_percent', 'mem_percent', 'load1')
            for item in metrics
        ),
        'source': source,
    }


def get_system_series(range_key: str = DEFAULT_RANGE) -> dict:
    """Ряд CPU/RAM (%) для графика (часовые/минутные бакеты)."""
    key, cfg = _range_settings(range_key)
    end = timezone.now()
    start = _minute_start(end - timedelta(minutes=cfg['minutes']))
    if cfg['resolution'] == 'hour':
        start = _hour_start(start)
        trunc = TruncHour
        step = timedelta(hours=1)
    else:
        trunc = TruncMinute
        step = timedelta(minutes=1)

    rows = []
    if not _disabled:
        try:
            rows = list(
                SystemMetric.objects
                .filter(
                    bucket_start__gte=start,
                    kind='gauge',
                    metric__in=('cpu_percent', 'mem_percent'),
                )
                .annotate(bucket=trunc('bucket_start'))
                .values('bucket', 'metric')
                .annotate(value=Max('value'))
                .order_by('bucket')
            )
        except (ProgrammingError, OperationalError) as exc:
            _disable_on_error(exc)

    per_bucket = {}
    for row in rows:
        point = per_bucket.setdefault(
            row['bucket'], {'cpu_percent': None, 'mem_percent': None},
        )
        point[row['metric']] = round(row['value'], 1)

    points = []
    bucket = start if cfg['resolution'] == 'hour' else _minute_start(start)
    last = _hour_start(end) if cfg['resolution'] == 'hour' else _minute_start(end)
    while bucket <= last:
        point = per_bucket.get(bucket)
        if point is not None:
            points.append({
                'bucket': bucket.isoformat(),
                'cpu_percent': point['cpu_percent'],
                'mem_percent': point['mem_percent'],
            })
        bucket += step

    return {
        'range': key,
        'resolution': cfg['resolution'],
        'available': bool(points),
        'points': points,
    }


def get_dashboard_data(range_key: str = DEFAULT_RANGE) -> dict:
    """Всё для дашборда: ряд, сводка, топы, внешние вызовы, пороги."""
    key, cfg = _range_settings(range_key)
    series = get_series(key)
    summary = get_window_stats(cfg['minutes'])
    top = get_top_groups(key)
    external = get_external_stats(cfg['minutes'])
    system = get_system_stats(cfg['minutes'])
    system_series = get_system_series(key)
    queue = get_queue_stats()

    summary = dict(summary)
    for field in ('start', 'end'):
        if summary.get(field) is not None:
            summary[field] = summary[field].isoformat()
    for field in ('start', 'end'):
        if external.get(field) is not None:
            external[field] = external[field].isoformat()
    for field in ('start', 'end'):
        if system.get(field) is not None:
            system[field] = system[field].isoformat()

    return {
        'range': key,
        'range_label': cfg['label'],
        'resolution': series['resolution'],
        'source': series['source'],
        'series': series['points'],
        'summary': summary,
        'top_groups': top['by_count'],
        'top_slow': top['by_p95'],
        'external': external,
        'system': system,
        'system_series': system_series,
        'queue': queue,
        'retention_days': retention_days(),
        'generated_at': timezone.now().isoformat(),
        'thresholds': {
            'requests_per_hour': HIGH_LOAD_THRESHOLD,
            'p95_ms': p95_threshold_ms(),
            'error_rate': error_rate_threshold(),
        },
    }


# ==========================================
# Очистка.
# ==========================================

def prune_old_metrics() -> dict:
    """Удалить метрики старше `REQUEST_METRICS_RETENTION_DAYS`."""
    days = retention_days()
    cutoff = timezone.now() - timedelta(days=days)
    if _disabled:
        return {
            'is_error': True,
            'deleted': 0,
            'days': days,
            'message': 'Метрики запросов отключены: таблица недоступна',
        }
    try:
        deleted, _details = (
            RequestMetric.objects.filter(bucket_start__lt=cutoff).delete()
        )
        deleted_external, _details = (
            ExternalCallMetric.objects.filter(bucket_start__lt=cutoff).delete()
        )
        deleted += deleted_external
        deleted_system, _details = (
            SystemMetric.objects.filter(bucket_start__lt=cutoff).delete()
        )
        deleted += deleted_system
    except (ProgrammingError, OperationalError) as exc:
        _disable_on_error(exc)
        return {
            'is_error': True,
            'deleted': 0,
            'days': days,
            'message': f'Метрики запросов недоступны: {exc}',
        }
    message = (
        f'Удалено метрик запросов и внешних вызовов: {deleted} '
        f'(старше {days} дн.)'
    )
    logger.info(message)
    return {'is_error': False, 'deleted': deleted, 'days': days,
            'message': message}


def _flush_at_exit() -> None:
    """Best-effort: не терять последний интервал при остановке процесса."""
    import sys

    # В тестах БД уже может быть удалена — не шумим трейсбеком.
    if 'test' in sys.argv:
        return
    try:
        if _rows or _external_rows or _system_rows:
            flush_metrics()
    except Exception:  # noqa: BLE001 — на выходе уже нечего чинить
        pass


atexit.register(_flush_at_exit)
