# app_helper/external.py

"""
Безопасный вызов внешних сервисов (ЧЗ/TrueAPI, подписи, заводы).

Обёртка ловит исключения, логирует и пишет событие в живой лог, не позволяя
сбою внешнего контура уронить запрос пользователя. Предназначена для мест,
где результат не критичен для ответа (проверки, диагностика).

Повторяющиеся одинаковые сбои не пишутся в EventLog и error-лог чаще, чем
раз в `EVENT_DEDUP_SECONDS` (иначе упавший внешний сервис забивает журнал).
"""

import hashlib
import logging

from django.core.cache import cache

from app_event.utils import log_event

logger = logging.getLogger(__name__)

# Окно подавления одинаковых событий/ошибок (секунды).
EVENT_DEDUP_SECONDS = 300


def _should_record(module: str, message: str) -> bool:
    """True, если такое событие ещё не писали в последние 5 минут."""
    digest = hashlib.md5(f'{module}:{message}'.encode('utf-8')).hexdigest()
    key = f'eventlog:dedup:{digest}'
    try:
        return cache.add(key, 1, EVENT_DEDUP_SECONDS)
    except Exception:  # noqa: BLE001 — кэш не должен ломать обработку
        return True


def safe_external_call(func, *args, module='cz', event_message=None, **kwargs):
    """
    Вызывает func(*args, **kwargs), перехватывая исключения.

    :return: (result, error) — result при успехе, error (str) при сбое.
    """
    try:
        return func(*args, **kwargs), None
    except Exception as e:
        message = event_message or f'Сбой внешнего сервиса: {func.__name__}'
        detail = f'{message}: {e}'
        if _should_record(module, detail):
            logger.error(detail, exc_info=True)
            try:
                log_event(module=module, level='warning', message=detail[:255])
            except Exception:
                pass
        else:
            logger.debug(f'Повторный сбой (подавлен): {detail}')
        return None, str(e)
