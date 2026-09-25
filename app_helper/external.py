# app_helper/external.py

"""
Безопасный вызов внешних сервисов (ЧЗ/TrueAPI, подписи, заводы).

Обёртка ловит исключения, логирует и пишет событие в живой лог, не позволяя
сбою внешнего контура уронить запрос пользователя. Предназначена для мест,
где результат не критичен для ответа (проверки, диагностика).
"""

import logging

from app_event.utils import log_event

logger = logging.getLogger(__name__)


def safe_external_call(func, *args, module='cz', event_message=None, **kwargs):
    """
    Вызывает func(*args, **kwargs), перехватывая исключения.

    :return: (result, error) — result при успехе, error (str) при сбое.
    """
    try:
        return func(*args, **kwargs), None
    except Exception as e:
        message = event_message or f'Сбой внешнего сервиса: {func.__name__}'
        logger.error(f'{message}: {e}', exc_info=True)
        try:
            log_event(module=module, level='warning', message=f'{message}: {e}')
        except Exception:
            pass
        return None, str(e)
