# app_scheduler/progress.py

"""
Прогресс выполнения периодических задач.

Задача (в воркере) пишет сюда этап и счётчики, а окно «Фоновые задачи»
(веб-процесс) читает и показывает. Хранится в БД, поэтому видно
кросс-процессно.

Все операции «мягкие»: если миграция ещё не применена (таблицы нет),
прогресс просто не записывается — сама задача не падает.
"""

import logging

logger = logging.getLogger(__name__)


def set_task_progress(
        task_name: str,
        *,
        phase: str = '',
        current: int = None,
        total: int = None,
        message: str = '',
) -> None:
    """Обновляет (или создаёт) прогресс задачи. Идемпотентно."""
    from app_scheduler.models import TaskProgress

    try:
        TaskProgress.objects.update_or_create(
            task_name=task_name,
            defaults={
                'phase': (phase or '')[:150],
                'current': current,
                'total': total,
                'message': (message or '')[:500],
            },
        )
    except Exception:
        logger.warning(
            'Не удалось записать прогресс задачи %s', task_name, exc_info=True,
        )


def get_task_progress(task_name: str):
    """Возвращает прогресс задачи (dict) или None."""
    from app_scheduler.models import TaskProgress

    try:
        row = TaskProgress.objects.filter(task_name=task_name).first()
    except Exception:
        return None
    if row is None:
        return None

    percent = None
    if row.current is not None and row.total:
        percent = max(0, min(100, int(row.current / row.total * 100)))

    return {
        'phase': row.phase,
        'current': row.current,
        'total': row.total,
        'message': row.message,
        'percent': percent,
        'updated_at': row.updated_at.strftime('%d.%m.%Y %H:%M:%S'),
    }


def clear_task_progress(task_name: str) -> None:
    """Удаляет запись прогресса (задача завершилась)."""
    from app_scheduler.models import TaskProgress

    try:
        TaskProgress.objects.filter(task_name=task_name).delete()
    except Exception:
        logger.warning(
            'Не удалось очистить прогресс задачи %s', task_name, exc_info=True,
        )
