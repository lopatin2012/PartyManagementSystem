# app_cz/services/nk_sync_state.py

"""
Сквозное (межпроцессное) состояние синхронизации Национального каталога.

Задача: ручная синхронизация (web) и фоновая задача (worker) не должны
выполняться одновременно — иначе суммарные запросы к True API пробивают
документированный лимит /nk/product (10 запросов за 5 минут) и возникают
ошибки 400, а параллельные записи в NationalCatalogProduct дают конфликты.

Основной вариант — блокировка на БД (SELECT ... FOR UPDATE строки-синглтона
`NKSyncState`): работает между процессами, прогресс виден из любого процесса.

ВАЖНО: миграция `NKSyncState` (app_cz/migrations/0004_*) gitignored и может
отсутствовать на конкретной БД (например, в Docker-развёртывании). Поэтому
модуль проверяет наличие таблицы и при её отсутствии НАСТОЯТЕЛЬНО переключается
на in-memory состояние (per-process): никакие 500-е не возникают, страница НК,
ручная синхронизация и окно «Фоновые задачи» работают как раньше. После
применения миграции автоматически включается полноценный межпроцессный режим.
"""

import logging
import threading
from datetime import timedelta

from django.utils import timezone

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

# Формат прогресса (единый для DB и in-memory вариантов).
DEFAULT_PROGRESS = {
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

# ==========================================
# Определение доступности таблицы NKSyncState.
# ==========================================

_DB_AVAILABLE = None  # None — ещё не проверяли.


def db_state_available() -> bool:
    """Есть ли таблица NKSyncState в БД (без 500 при её отсутствии)."""
    global _DB_AVAILABLE
    if _DB_AVAILABLE is None:
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.app_cz_nksyncstate')"
                )
                row = cursor.fetchone()
            _DB_AVAILABLE = bool(row and row[0])
        except Exception:
            _DB_AVAILABLE = False
    return _DB_AVAILABLE


# ==========================================
# In-memory fallback (per-process).
# ==========================================

_mem_lock = threading.Lock()
_mem_state = {
    'is_running': False,
    'started_by': '',
    'started_at': None,
    'finished_at': None,
    'message': '',
    'progress': dict(DEFAULT_PROGRESS, running=False),
}


def _try_start_mem(started_by: str) -> tuple:
    with _mem_lock:
        state = _mem_state
        if state['is_running']:
            stale = (
                state['started_at']
                and timezone.now() - state['started_at'] > LOCK_STALE_TIMEOUT
            )
            if not stale:
                who = STARTED_BY_DISPLAY.get(state['started_by'], state['started_by'])
                started = (
                    state['started_at'].strftime('%d.%m.%Y %H:%M:%S')
                    if state['started_at'] else 'неизвестно'
                )
                return False, (
                    f'Синхронизация Национального каталога уже выполняется '
                    f'{who} (начата {started}). Дождитесь завершения.'
                )
        state['is_running'] = True
        state['started_by'] = started_by
        state['started_at'] = timezone.now()
        state['finished_at'] = None
        state['message'] = ''
        state['progress'] = dict(DEFAULT_PROGRESS)
        return True, ''


def _update_progress_mem(**kwargs) -> None:
    with _mem_lock:
        progress = dict(_mem_state['progress'] or {})
        progress.update(kwargs)
        _mem_state['progress'] = progress


def _finish_mem(message: str = '') -> None:
    with _mem_lock:
        _mem_state['is_running'] = False
        _mem_state['finished_at'] = timezone.now()
        _mem_state['message'] = message
        progress = dict(_mem_state['progress'] or {})
        progress['running'] = False
        _mem_state['progress'] = progress


def _get_state_mem() -> dict:
    with _mem_lock:
        state = _mem_state
        return {
            'running': state['is_running'],
            'started_by': state['started_by'],
            'started_by_display': STARTED_BY_DISPLAY.get(
                state['started_by'], state['started_by'] or ''
            ),
            'started_at': (
                state['started_at'].strftime('%d.%m.%Y %H:%M:%S')
                if state['started_at'] else None
            ),
            'finished_at': (
                state['finished_at'].strftime('%d.%m.%Y %H:%M:%S')
                if state['finished_at'] else None
            ),
            'message': state['message'],
            'progress': state['progress'] or {},
        }


# ==========================================
# DB-реализация.
# ==========================================


def _get_state_row():
    """Возвращает строку-синглтон состояния (создаёт при первом обращении)."""
    from django.db import transaction
    from app_cz.models import NKSyncState
    state, _ = NKSyncState.objects.get_or_create(id=1)
    return state


def _try_start_db(started_by: str) -> tuple:
    from django.db import transaction
    from app_cz.models import NKSyncState

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
        state.progress = dict(DEFAULT_PROGRESS)
        state.save()
        return True, ''


def _update_progress_db(**kwargs) -> None:
    state = _get_state_row()
    progress = dict(state.progress or {})
    progress.update(kwargs)
    state.progress = progress
    state.save(update_fields=['progress', 'updated_at'])


def _finish_db(message: str = '') -> None:
    state = _get_state_row()
    state.is_running = False
    state.finished_at = timezone.now()
    state.message = message
    progress = dict(state.progress or {})
    progress['running'] = False
    state.progress = progress
    state.save(update_fields=[
        'is_running', 'finished_at', 'message', 'progress', 'updated_at'
    ])


def _get_state_db() -> dict:
    state = _get_state_row()
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


# ==========================================
# Публичный API (выбирает DB или in-memory автоматически).
# ==========================================


def _db_guard(fn_db, fn_mem, *args, **kwargs):
    """Выполняет DB-вариант, при недоступности таблицы — in-memory fallback.

    Дополнительная защита поверх db_state_available(): если таблица исчезла
    между проверкой и запросом (например, процесс был запущен до применения
    миграции), ловим ProgrammingError/OperationalError и переключаемся на
    память, а не роняем задачу с 500.
    """
    if not db_state_available():
        return fn_mem(*args, **kwargs)
    try:
        return fn_db(*args, **kwargs)
    except Exception as e:
        from django.db import ProgrammingError, OperationalError
        if isinstance(e, (ProgrammingError, OperationalError)):
            logger.warning(
                'NKSyncState недоступен в БД (%s) — переключение на in-memory',
                e,
            )
            global _DB_AVAILABLE
            _DB_AVAILABLE = False
            return fn_mem(*args, **kwargs)
        raise


def try_start_sync(started_by: str) -> tuple[bool, str]:
    """
    Пытается захватить блокировку синхронизации.

    :param started_by: 'manual' или 'scheduler'.
    :return: (True, '') при успешном захвате, иначе (False, сообщение).
    """
    return _db_guard(_try_start_db, _try_start_mem, started_by)


def update_sync_progress(**kwargs) -> None:
    """Обновляет поля прогресса (без захвата блокировки)."""
    _db_guard(_update_progress_db, _update_progress_mem, **kwargs)


def finish_sync(message: str = '') -> None:
    """Снимает блокировку и фиксирует сообщение о завершении."""
    _db_guard(_finish_db, _finish_mem, message)


def get_sync_state() -> dict:
    """Словарь состояния для отображения на странице / в окне задач."""
    return _db_guard(_get_state_db, _get_state_mem)


class SyncProgress(dict):
    """
    Словарь прогресса, который при каждом изменении пишется в общее состояние
    (NKSyncState в БД либо in-memory fallback), чтобы прогресс был виден на
    странице «Национальный каталог» и в окне «Фоновые задачи» из любого процесса.
    """

    def __init__(self, **defaults):
        super().__init__(dict(DEFAULT_PROGRESS))
        self.update(defaults)
        self._persist()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._persist()

    def _persist(self):
        update_sync_progress(**dict(self))