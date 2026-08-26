# app_cz/services/rate_limit.py

"""
Ограничитель частоты запросов к API «Честного Знака».

Соблюдает документированные лимиты True API (True_API_GIS_MT.txt, v.708) и
СУЗ-Облако (API_СУЗ_3.0.txt):

- /nk/product — не более 10 запросов за 5 минут, до 25 good_id/gtin за запрос;
- остальные методы Национального каталога — не более 10 запросов в секунду;
- общий лимит True API — не более 50 запросов в секунду от участника оборота;
- СУЗ-Облако — не более 100 запросов в секунду на пару «IP и omsId».

Ограничитель потокобезопасен: потоки (воркеры, синхронизация НК в отдельном
потоке и т.п.) совместно используют общие счётчики, поэтому одновременные
синхронизации не пробивают лимит.
"""

import threading
import time
from collections import deque


class RateLimiter:
    """Потокобезопасный ограничитель частоты (скользящее окно).

    Для каждого ключа разрешает не более `limit` обращений за последние
    `window_seconds` секунд. Метод `wait` блокирует поток до освобождения слота.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = {}

    def wait(self, key, limit, window_seconds):
        """Блокирует вызывающий поток до разрешения очередного запроса."""
        while True:
            delay = self._acquire(key, limit, window_seconds)
            if delay <= 0:
                return
            time.sleep(delay)

    def _acquire(self, key, limit, window_seconds) -> float:
        """Регистрирует запрос; возвращает время ожидания (0 — можно слать)."""
        with self._lock:
            now = time.monotonic()
            timestamps = self._requests.setdefault(key, deque())
            cutoff = now - window_seconds
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) < limit:
                timestamps.append(now)
                return 0.0
            return timestamps[0] + window_seconds - now


# Общий ограничитель True API (один на процесс).
true_api_limiter = RateLimiter()

# Лимиты True API по эндпоинтам (True_API_GIS_MT.txt).
# /nk/product — 10 запросов за 5 минут (строго документировано).
NK_PRODUCT_LIMIT = (10, 300)
# Остальные методы НК — 10 запросов в секунду (консервативно).
NK_OTHER_LIMIT = (10, 1)
# Общий лимит True API — 50 запросов в секунду от участника оборота.
TRUE_API_GLOBAL_LIMIT = (50, 1)


def wait_nk_endpoint(endpoint: str):
    """Ожидает разрешения для обращения к методу Национального каталога."""
    if endpoint == '/nk/product':
        limit, window = NK_PRODUCT_LIMIT
    else:
        limit, window = NK_OTHER_LIMIT
    true_api_limiter.wait(f'nk:{endpoint}', limit, window)
    true_api_limiter.wait('true_api:global', *TRUE_API_GLOBAL_LIMIT)
