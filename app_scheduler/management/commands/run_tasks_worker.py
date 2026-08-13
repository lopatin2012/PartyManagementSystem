# app_scheduler/management/commands/run_tasks_worker.py

"""
Воркер для выполнения фоновых задач django-tasks.

Запуск:
    python manage.py run_tasks_worker
    python manage.py run_tasks_worker --interval 5  # проверка каждые 5 секунд
    python manage.py run_tasks_worker --once

"""

import logging
import socket
import time
import uuid

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Запускает воркер для выполнения фоновых задач django-tasks'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=5,
            help='Интервал опроса очереди в секундах (по умолчанию 5)',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Обработать все готовые задачи один раз и выйти',
        )

    def handle(self, *args, **options):
        from django_tasks_db.models import DBTaskResult

        poll_interval = options['interval']
        run_once = options['once']

        # Уникальный ID воркера (hostname + uuid)
        worker_id = f'{socket.gethostname()}:{uuid.uuid4().hex[:8]}'

        self.stdout.write(self.style.SUCCESS(
            f'Воркер запущен. ID: {worker_id}. Интервал опроса: {poll_interval} сек.'
        ))

        try:
            while True:
                processed = self._process_one_task(DBTaskResult, worker_id)

                if run_once:
                    self.stdout.write(self.style.SUCCESS(
                        f'Режим --once: обработано {processed} задач, выход'
                    ))
                    break

                if processed == 0:
                    time.sleep(poll_interval)

        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\nВоркер остановлен'))

    def _process_one_task(self, DBTaskResult, worker_id: str) -> int:
        """
        Берёт одну готовую задачу из очереди, выполняет и сохраняет результат.
        Возвращает количество обработанных задач (0 или 1).
        """
        task_result = None

        # 1. Берём задачу с блокировкой строки (skip_locked для многопроцессности).
        with transaction.atomic():
            task_result = DBTaskResult.objects.ready().get_locked()
            if not task_result:
                return 0
            # Забираем задачу — помечаем как RUNNING
            task_result.claim(worker_id)

        # 2. Вне транзакции выполняем задачу (чтобы не держать лок слишком долго).
        task_path = task_result.task_path
        self.stdout.write(
            f'[{timezone.now():%H:%M:%S}] ▶ Выполняется: {task_path} (ID: {task_result.id})'
        )
        logger.info(f'Воркер {worker_id}: начало выполнения задачи {task_result.id} ({task_path})')

        try:
            # Импортируем оригинальную функцию задачи
            original_func = self._get_task_function(task_result)

            # Получаем аргументы из JSON-поля args_kwargs
            args = task_result.args_kwargs.get('args', []) or []
            kwargs = task_result.args_kwargs.get('kwargs', {}) or {}

            # Выполняем задачу
            result = original_func(*args, **kwargs)

            # 3. Успешное завершение
            task_result.set_successful(result)

            logger.info(f'Воркер: задача {task_result.id} выполнена успешно')
            self.stdout.write(self.style.SUCCESS(
                f'[{timezone.now():%H:%M:%S}] ✓ Завершена: {task_path} (ID: {task_result.id})'
            ))
            return 1

        except Exception as exc:
            # 4. Ошибка выполнения
            task_result.set_failed(exc)

            logger.error(
                f'Воркер: задача {task_result.id} завершилась с ошибкой: {exc}',
                exc_info=True
            )
            self.stdout.write(self.style.ERROR(
                f'[{timezone.now():%H:%M:%S}] ✗ Ошибка: {task_path} (ID: {task_result.id}): {exc}'
            ))
            return 1

    def _get_task_function(self, task_result):
        """
        Возвращает оригинальную функцию задачи по task_path.
        Поддерживает как обычные функции, так и обёрнутые через @task().
        """
        from django.utils.module_loading import import_string

        task_path = task_result.task_path
        task_obj = import_string(task_path)

        # Если это обёртка @task(), берём оригинальную функцию
        if hasattr(task_obj, 'func'):
            return task_obj.func

        return task_obj
