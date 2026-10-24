"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import asyncio
import logging
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# uvicorn настраивает этот логгер с выводом в консоль; собственный
# `config.asgi` INFO-уровень по умолчанию никуда не пишется (у root нет handler).
logger = logging.getLogger('uvicorn.error')


def _handle_loop_exception(loop, context):
    """
    Понятная обработка отмены запросов при остановке/перезапуске сервера.

    При SIGTERM/рестарте uvicorn отменяет in-flight запросы, а ``asyncio.shield``
    логирует это как «CancelledError exception in shielded future» с большим
    стектрейсом. Это штатное завершение, а не сбой — пишем одну строку INFO.
    """
    exc = context.get('exception')
    message = str(context.get('message') or '')
    if isinstance(exc, asyncio.CancelledError) or 'shielded future' in message:
        logger.info(
            'Остановка сервера: in-flight запрос отменён (%s) — '
            'штатное завершение, не ошибка.',
            message or 'CancelledError',
        )
        return
    loop.default_exception_handler(context)


class _ExceptionHandlerASGI:
    """Ставит обработчик исключений event loop перед обработкой запроса."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        asyncio.get_running_loop().set_exception_handler(_handle_loop_exception)
        return await self.app(scope, receive, send)


application = _ExceptionHandlerASGI(get_asgi_application())
