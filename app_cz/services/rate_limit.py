# app_cz/services/rate_limit.py

"""
Ограничитель частоты запросов к API «Честного Знака».

Соблюдает документированные лимиты True API (True_API_GIS_MT.txt, v.708) и
СУЗ-Облако (API_СУЗ_3.0.txt):

- /nk/product — не более 10 запросов за 5 минут, до 25 good_id/gtin за запрос;
- остальные методы Национального каталога — не более 10 запросов в секунду;
- общий лимит True API — не более 50 запросов в секунду от участника оборота;
- СУЗ-Облако — не более 100 запросов в секунду на пару «IP и omsId».

Ограничитель потокобезопасен и равномерно «размазывает» запросы (min interval =
window / limit), чтобы не было резких всплесков и долгих пауз после первого
порыва (это «замораживало» прогресс синхронизации). Блокировка на время
ожидания видна через callback `on_wait` (прогресс синхронизации).

ВАЖНО: лимитер живёт в рамках одного процесса. Ручная синхронизация (web) и
фоновая задача (worker) разделены межпроцессной блокировкой NKSyncState
(app_cz/services/nk_sync_state.py), поэтому запросы к True API всегда идут
только из одного процесса одновременно.
"""

import threading
import time
from typing import Callable, Optional


class RateLimiter:
    """Потокобезопасный ограничитель частоты с равномерным шагом.

    Для каждого ключа разрешает не более `limit` обращений за последние
    `window_seconds` секунд, выдерживая паузу не меньше window/limit между
    последовательными запросами. Метод `wait` блокирует поток до разрешения.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # key -> время последнего разрешённого запроса (time.monotonic).
        self._last = {}

    def wait(
        self,
        key: str,
        limit: int,
        window_seconds: float,
        on_wait: Optional[Callable[[float], None]] = None,
    ):
        """Блокирует поток до разрешения очередного запроса.

        :param on_wait: вызывается с временем ожидания (сек) перед паузой,
                        чтобы обновить прогресс (например, «ожидание лимита»).
        """
        min_interval = window_seconds / limit
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(key)
                if last is None or now - last >= min_interval:
                    self._last[key] = now
                    return
                delay = min_interval - (now - last)
            if on_wait is not None:
                on_wait(delay)
            time.sleep(delay)


# Общий ограничитель True API (один на процесс).
true_api_limiter = RateLimiter()

# Лимиты True API по эндпоинтам (True_API_GIS_MT.txt).
# /nk/product — 10 запросов за 5 минут (строго документировано).
NK_PRODUCT_LIMIT = (10, 300)
# Остальные методы НК — 10 запросов в секунду (консервативно).
NK_OTHER_LIMIT = (10, 1)
# Общий лимит True API — 50 запросов в секунду от участника оборота.
TRUE_API_GLOBAL_LIMIT = (50, 1)


def wait_nk_endpoint(endpoint: str, on_wait: Optional[Callable[[float], None]] = None):
    """Ожидает разрешения для обращения к методу Национального каталога."""
    if endpoint == '/nk/product':
        limit, window = NK_PRODUCT_LIMIT
    else:
        limit, window = NK_OTHER_LIMIT
    true_api_limiter.wait(f'nk:{endpoint}', limit, window, on_wait=on_wait)
    true_api_limiter.wait('true_api:global', *TRUE_API_GLOBAL_LIMIT, on_wait=on_wait)
