# app_scheduler/management/commands/run_scheduler.py


"""
Планировщик периодических задач.

Запуск:
    python manage.py run_scheduler
    python manage.py run_scheduler --interval 60  # проверка каждую минуту
"""

import time
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


# Константы для расписания
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(weeks=1)

# ==========================================
# РАСПИСАНИЕ ЗАДАЧ
# ==========================================
# Формат: (task, interval_seconds, description)
SCHEDULE = [
    # Работа с ЧЗ.
    # Каждые 6 часов — обновление токена СУЗ (он живёт 8 часов)
    ('refresh_suz_token', 6 * HOUR, 'Обновление токена СУЗ'),

    # Работа со статусами УИП.
    ('cleanup_expired_reserved', 1 * DAY, 'Удаление УИП без регистрации (30 дней)'),
    ('close_unused_registered', 1 * DAY, 'Закрытие УИП без использования (3 дня)'),
    ('archive_stale_closed', 1 * DAY, 'Архивация закрытых УИП (30+ дней)'),

    # Очистка данных.
    ('cleanup_old_logs', 1 * DAY, 'Очистка старых логов статусов (700 дней)'),

    # Синхронизация с внешним сервисом (Молвест.Маркировка).
    # Задания и коды маркировки.
    ('sync_external_tasks', 30 * MINUTE, 'Синхронизация заданий и кодов с внешним сервисом'),

    # Мониторинг резерва УИП.
    # >50% — предупреждение, >80% — тревога, >90% — снятие устаревших УИП.
    ('check_uip_reserve', 1 * DAY, 'Проверка резерва УИП и уведомления по почте'),

    # # Каждые 30 минут — синхронизация УИП с ЧЗ
    # ('sync_parties', 30 * 60, 'Синхронизация УИП с ЧЗ'),
    #
    # # Раз в сутки в 3:00 — архивация УИП
    # ('archive_stale_uips', 24 * 3600, 'Архивация УИП без активности'),
    #
    # # Раз в неделю — очистка старых логов
    # ('cleanup_old_logs', 7 * 24 * 3600, 'Очистка старых логов статусов'),
]


class Command(BaseCommand):
    help = 'Запускает планировщик периодических задач django-tasks'

    def _get_next_run_info(self, task_map, DBTaskResult, TaskResultStatus):
        """
        Возвращает список словарей с информацией о следующем запуске каждой задачи.
        Используется при старте планировщика и в API.
        """
        now = timezone.now()
        schedule_info = []

        for name, interval, description in SCHEDULE:
            task_func = task_map.get(name)
            if not task_func:
                continue

            original_func = task_func.func
            task_path = f'{original_func.__module__}.{original_func.__name__}'

            # Ищем последнее завершённое выполнение.
            last_run = (
                DBTaskResult.objects
                .filter(task_path=task_path)
                .filter(status__in=[TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED])
                .order_by('-finished_at')
                .first()
            )

            if last_run and last_run.finished_at:
                next_run = last_run.finished_at + interval
                # Если время уже прошло — задача будет запущена при следующей проверке.
                if next_run <= now:
                    next_run_display = 'сейчас (при следующей проверке)'
                    next_run_dt = now
                else:
                    next_run_display = next_run.strftime('%d.%m.%Y %H:%M:%S')
                    next_run_dt = next_run
                last_run_display = last_run.finished_at.strftime('%d.%m.%Y %H:%M:%S')
            else:
                next_run_display = 'первый запуск (сейчас)'
                next_run_dt = now
                last_run_display = 'никогда'

            schedule_info.append({
                'name': name,
                'description': description,
                'interval': str(interval),
                'interval_display': self._format_interval(int(interval.total_seconds())),
                'last_run': last_run_display,
                'next_run': next_run_display,
                'next_run_dt': next_run_dt,
            })

        return schedule_info

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=60,
            help='Интервал проверки расписания в секундах (по умолчанию 60)',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Выполнить все задачи один раз и выйти (для тестирования)',
        )

    def handle(self, *args, **options):
        from app_scheduler.tasks import (
            refresh_suz_token_task,
            cleanup_expired_reserved_uips_task,
            close_unused_registered_uips_task,
            archive_stale_closed_uips_task,
            cleanup_old_logs_task,
            sync_external_tasks_task,
            check_uip_reserve_task,
        )
        from django_tasks_db.models import DBTaskResult
        from django_tasks.base import TaskResultStatus

        # Маппинг имён задач на функции
        task_map = {
            'refresh_suz_token': refresh_suz_token_task,
            'cleanup_expired_reserved': cleanup_expired_reserved_uips_task,
            'close_unused_registered': close_unused_registered_uips_task,
            'archive_stale_closed': archive_stale_closed_uips_task,
            'cleanup_old_logs': cleanup_old_logs_task,
            'sync_external_tasks': sync_external_tasks_task,
            'check_uip_reserve': check_uip_reserve_task,
        }

        check_interval = options['interval']
        run_once = options['once']

        self.stdout.write(self.style.SUCCESS(
            f'\nПланировщик запущен. Интервал проверки: {check_interval} сек.\n'
        ))

        # === Вывод расписания с временами следующего запуска ===
        self.stdout.write(self.style.MIGRATE_HEADING('Расписание задач:'))
        self.stdout.write('─' * 100)
        self.stdout.write(f'{"Задача":<45} {"Интервал":<12} {"Последний запуск":<22} {"Следующий запуск":<22}')
        self.stdout.write('─' * 100)

        schedule_info = self._get_next_run_info(task_map, DBTaskResult, TaskResultStatus)
        for info in schedule_info:
            self.stdout.write(
                f'{info["description"]:<45} '
                f'{info["interval_display"]:<12} '
                f'{info["last_run"]:<22} '
                f'{info["next_run"]:<22}'
            )

        self.stdout.write('─' * 100)
        self.stdout.write('')

        try:
            while True:
                self._check_and_enqueue(task_map, DBTaskResult, TaskResultStatus)
                if run_once:
                    self.stdout.write(self.style.SUCCESS('Режим --once: выход'))
                    break
                time.sleep(check_interval)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\nПланировщик остановлен'))

    def _check_and_enqueue(self, task_map, DBTaskResult, TaskResultStatus):
        """Проверяет каждую задачу в расписании и ставит её в очередь, если нужно."""
        now = timezone.now()

        for name, interval, description in SCHEDULE:
            task_func = task_map.get(name)
            if not task_func:
                continue

            # Получаем путь к задаче через оригинальную функцию.
            original_func = task_func.func
            task_path = f'{original_func.__module__}.{original_func.__name__}'

            # Защита от дублирования.
            pending = (
                DBTaskResult.objects
                .filter(task_path=task_path)
                .filter(status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING])
                .exists()
            )
            if pending:
                logger.debug(f'Планировщик: {description} уже в очереди, пропускаем')
                continue

            # Ищем последнее завершённое выполнение.
            last_run = (
                DBTaskResult.objects
                .filter(task_path=task_path)
                .filter(status__in=[TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED])
                .order_by('-finished_at')
                .first()
            )

            # Если это первый запуск — выполняем сразу.
            if not last_run:
                self._enqueue(task_func, description, 'первый запуск')
                continue

            # Проверяем, прошло ли достаточно времени
            time_since = (now - last_run.finished_at).total_seconds()
            if time_since >= interval.total_seconds():
                self._enqueue(
                    task_func,
                    description,
                    f'прошло {self._format_interval(int(time_since))}',
                )
            else:
                time_left = interval.total_seconds() - time_since
                logger.debug(
                    f'Планировщик: {description} — ещё рано '
                    f'(осталось {self._format_interval(int(time_left))})'
                )

    def _enqueue(self, task_func, description, reason):
        """Ставит задачу в очередь и логирует."""
        try:
            result = task_func.enqueue()
            logger.info(f'Планировщик: {description} поставлена в очередь ({reason})')
            self.stdout.write(
                f'[{timezone.now():%H:%M:%S}] ✓ {description} ({reason})'
            )
        except Exception as e:
            logger.error(f'Планировщик: ошибка при постановке {description}: {e}')
            self.stderr.write(self.style.ERROR(
                f'[{timezone.now():%H:%M:%S}] ✗ Ошибка: {description}: {e}'
            ))

    def _format_interval(self, seconds: int) -> str:
        """Форматирует секунды в читаемый вид."""
        if seconds < 60:
            return f'{seconds} сек'
        if seconds < 3600:
            return f'{seconds // 60} мин'
        if seconds < 86400:
            hours = seconds // 3600
            return f'{hours} ч'
        days = seconds // 86400
        return f'{days} дн'
