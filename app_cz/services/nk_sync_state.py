# app_cz/services/nk_sync_state.py

"""
Сквозное (межпроцессное) состояние синхронизации Национального каталога.

Задача: ручная синхронизация (web) и фоновая задача (worker) не должны
выполняться одновременно — иначе суммарные запросы к True API пробивают
документированный лимит /nk/product (10 запросов за 5 минут) и возникают
ошибки 400, а параллельные записи в NationalCatalogProduct дают конфликты.

Блокировка построена на БД (SELECT ... FOR UPDATE строки-синглтона):
работает между процессами в отличие от in-memory лока.
Прогресс хранится в той же строке, поэтому страница «Национальный каталог»
и окно «Фоновые задачи» видят актуальное состояние любого инициатора.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from app_cz.models import NKSyncState

logger = logging.getLogger(__name__)

# Максимальная длительность синхронизации, после которой блокировка считается
# «зависшей» (процесс упал) и может быть перехвачена другим инициатором.
# Учитывает медленный лимит /nk/product (10 req / 5 мин) на больших каталогах.
LOCK_STALE_TIMEOUT = timedelta(hours=4)

# Кто запускает синхронизацию (для отображения на странице / в окне задач).
STARTED_BY_DISPLAY = {
    'manual': 'вручную',
    'scheduler': 'фоновой задачей',
}


def get_state_row() -> NKSyncState:
    """Возвращает строку-синглтон состояния (создаёт при первом обращении)."""
    state, _ = NKSyncState.objects.get_or_create(id=1)
    return state


def try_start_sync(started_by: str) -> tuple[bool, str]:
    """
    Пытается захватить блокировку синхронизации.

    :param started_by: 'manual' или 'scheduler'.
    :return: (True, '') при успешном захвате, иначе (False, сообщение).
    """
    with transaction.atomic():
        # FOR UPDATE гарантирует сериализацию между процессами.
        state = NKSyncState.objects.select_for_update().filter(id=1).first()
        if state is None:
            state = NKSyncState(id=1)

        if state.is_running:
            stale = (
                state.started_at
                and timezone.now() - state.started_at > LOCK_STALE_TIMEOUT
            )
            if not stale:
                who = STARTED_BY_DISPLAY.get(state.started_by, state.started_by)
                started = (
                    state.started_at.strftime('%d.%m.%Y %H:%M:%S')
                    if state.started_at else 'неизвестно'
                )
                return False, (
                    f'Синхронизация Национального каталога уже выполняется '
                    f'{who} (начата {started}). Дождитесь завершения.'
                )

        # Свободно или «зависшая» блокировка — захватываем.
        state.is_running = True
        state.started_by = started_by
        state.started_at = timezone.now()
        state.finished_at = None
        state.message = ''
        state.progress = {
            'running': True,
            'total': 0,
            'done': 0,
            'created': 0,
            'updated': 0,
            'errors': 0,
            'current_name': '',
            'phase': 'list',
            'waiting': False,
            'error': None,
        }
        state.save()
        return True, ''


def update_sync_progress(**kwargs) -> None:
    """Обновляет поля прогресса в строке состояния (без захвата блокировки)."""
    state = get_state_row()
    progress = dict(state.progress or {})
    progress.update(kwargs)
    state.progress = progress
    state.save(update_fields=['progress', 'updated_at'])


def finish_sync(message: str = '') -> None:
    """Снимает блокировку и фиксирует сообщение о завершении."""
    state = get_state_row()
    state.is_running = False
    state.finished_at = timezone.now()
    state.message = message
    progress = dict(state.progress or {})
    progress['running'] = False
    state.progress = progress
    state.save(update_fields=[
        'is_running', 'finished_at', 'message', 'progress', 'updated_at'
    ])


def get_sync_state() -> dict:
    """Словарь состояния для отображения на странице / в окне задач."""
    state = get_state_row()
    return {
        'running': state.is_running,
        'started_by': state.started_by,
        'started_by_display': STARTED_BY_DISPLAY.get(
            state.started_by, state.started_by or ''
        ),
        'started_at': (
            state.started_at.strftime('%d.%m.%Y %H:%M:%S')
            if state.started_at else None
        ),
        'finished_at': (
            state.finished_at.strftime('%d.%m.%Y %H:%M:%S')
            if state.finished_at else None
        ),
        'message': state.message,
        'progress': state.progress or {},
    }


class SyncProgress(dict):
    """
    Словарь прогресса, который при каждом изменении пишется в NKSyncState.

    Позволяет передавать progress в sync_products/get_all_products как обычный
    dict, но при этом каждая запись (done/errors/current_name/phase/...) сразу
    сохраняется в БД — её видно на странице «Национальный каталог» и в окне
    «Фоновые задачи» из любого процесса.
    """

    def __init__(self, **defaults):
        super().__init__({
            'running': True,
            'total': 0,
            'done': 0,
            'created': 0,
            'updated': 0,
            'errors': 0,
            'current_name': '',
            'phase': 'list',
            'waiting': False,
            'error': None,
        })
        self.update(defaults)
        self._persist()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._persist()

    def _persist(self):
        update_sync_progress(**dict(self))