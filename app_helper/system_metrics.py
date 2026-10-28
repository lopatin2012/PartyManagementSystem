# app_helper/system_metrics.py

"""
Сэмплы инфраструктуры: Postgres и ОС.

Возвращают словарь `{metric: (kind, value)}`:

- `gauge` — мгновенное значение (CPU%, RAM%, load, соединения, размер БД);
- `counter` — приращение счётчика с прошлого сэмпла (xact/s, blks_read,
  temp_bytes).

Ошибки не пробрасываются: недоступный источник (например, `/proc` на Windows)
просто не даёт метрик. Сэмплирование вызывается при сбросе метрик запросов
(`app_helper.metrics.flush_metrics`) — то есть при наличии трафика.
"""

import logging
import os

from django.db import OperationalError, ProgrammingError, connection

logger = logging.getLogger(__name__)

_last_cpu_times = None
_last_db_counters = None


def _read_cpu_times():
    """(total, idle+iowait) из /proc/stat или None."""
    try:
        with open('/proc/stat', 'r', encoding='utf-8') as handle:
            line = handle.readline()
    except OSError:
        return None
    try:
        parts = [int(value) for value in line.split()[1:]]
    except (ValueError, IndexError):
        return None
    if len(parts) < 4:
        return None
    total = sum(parts)
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return total, idle


def _host_samples() -> dict:
    """CPU/RAM/load из /proc (только Linux; иначе пусто)."""
    global _last_cpu_times

    samples = {}

    cpu = _read_cpu_times()
    if cpu is not None:
        total, idle = cpu
        previous = _last_cpu_times
        _last_cpu_times = (total, idle)
        if previous is not None:
            total_delta = total - previous[0]
            idle_delta = idle - previous[1]
            if total_delta > 0:
                samples['cpu_percent'] = (
                    'gauge',
                    round(100 * (1 - idle_delta / total_delta), 1),
                )

    try:
        with open('/proc/meminfo', 'r', encoding='utf-8') as handle:
            info = {}
            for line in handle:
                if ':' in line:
                    key, value = line.split(':', 1)
                    info[key] = value.split()[0]
        total_mem = int(info.get('MemTotal', 0))
        available = int(info.get('MemAvailable', 0))
        if total_mem:
            samples['mem_percent'] = (
                'gauge',
                round(100 * (1 - available / total_mem), 1),
            )
    except (OSError, ValueError, IndexError):
        pass

    try:
        load1, load5, _load15 = os.getloadavg()
        samples['load1'] = ('gauge', round(load1, 2))
        samples['load5'] = ('gauge', round(load5, 2))
    except (OSError, AttributeError):
        pass

    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    samples['web_rss_mb'] = (
                        'gauge', round(int(line.split()[1]) / 1024, 1),
                    )
                    break
    except (OSError, ValueError, IndexError):
        pass

    return samples


def _db_samples() -> dict:
    """Postgres: активные соединения, размер БД, приращения счётчиков."""
    global _last_db_counters

    samples = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE state = 'active'), "
                "count(*) FILTER (WHERE state = 'idle in transaction'), "
                "count(*) FROM pg_stat_activity "
                "WHERE datname = current_database()"
            )
            active, idle_in_tx, total = cursor.fetchone()
            samples['db_active_connections'] = ('gauge', float(active or 0))
            samples['db_idle_in_tx'] = ('gauge', float(idle_in_tx or 0))
            samples['db_connections'] = ('gauge', float(total or 0))

            cursor.execute("SELECT current_setting('max_connections')::int")
            samples['db_max_connections'] = (
                'gauge', float(cursor.fetchone()[0] or 0),
            )

            cursor.execute("SELECT pg_database_size(current_database())")
            samples['db_size_mb'] = (
                'gauge', round(cursor.fetchone()[0] / 1024 / 1024, 1),
            )

            cursor.execute(
                "SELECT xact_commit, xact_rollback, blks_read, temp_bytes "
                "FROM pg_stat_database WHERE datname = current_database()"
            )
            row = cursor.fetchone()
    except (ProgrammingError, OperationalError) as exc:
        logger.debug(f'Не удалось снять метрики Postgres: {exc}')
        return samples

    commit, rollback, blks_read, temp_bytes = (
        int(value or 0) for value in row
    )
    previous = _last_db_counters
    _last_db_counters = (commit, rollback, blks_read, temp_bytes)
    if previous is not None:
        for metric, value, old in (
            ('db_xact_commit', commit, previous[0]),
            ('db_xact_rollback', rollback, previous[1]),
            ('db_blks_read', blks_read, previous[2]),
            ('db_temp_bytes', temp_bytes, previous[3]),
        ):
            delta = value - old
            if delta < 0:
                # Статистика Postgres перезапускалась — дельту не считаем.
                delta = 0
            samples[metric] = ('counter', float(delta))

    return samples


def collect_samples() -> dict:
    """Все доступные сэмплы {metric: (kind, value)}."""
    samples = {}
    try:
        samples.update(_db_samples())
    except Exception:  # noqa: BLE001 — сэмплы не должны ломать запрос
        logger.exception('Ошибка сэмплирования Postgres')
    try:
        samples.update(_host_samples())
    except Exception:  # noqa: BLE001
        logger.exception('Ошибка сэмплирования ОС')
    return samples
