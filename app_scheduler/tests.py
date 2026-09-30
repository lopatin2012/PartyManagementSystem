# app_scheduler/tests.py

"""Тесты логики планировщика периодических задач."""

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django_tasks.base import TaskResultStatus

from app_scheduler.management.commands.run_scheduler import (
    FAST_RETRY_INTERVAL,
    HOUR,
    SCHEDULE,
    _effective_interval,
)


class _FakeResult:
    """Минимальная заглушка DBTaskResult для проверки статуса."""

    def __init__(self, status):
        self.status = status


class EffectiveIntervalTests(SimpleTestCase):
    """Упавшие задачи из FAST_RETRY_TASKS повторяются раньше."""

    def test_failed_external_sync_retries_fast(self):
        result = _FakeResult(TaskResultStatus.FAILED)
        self.assertEqual(
            _effective_interval('sync_external_parties_codes', HOUR, result),
            FAST_RETRY_INTERVAL,
        )

    def test_successful_external_sync_uses_full_interval(self):
        result = _FakeResult(TaskResultStatus.SUCCESSFUL)
        self.assertEqual(
            _effective_interval('sync_external_parties_codes', HOUR, result),
            HOUR,
        )

    def test_other_tasks_are_not_fast_retried(self):
        result = _FakeResult(TaskResultStatus.FAILED)
        self.assertEqual(_effective_interval('refresh_suz_token', HOUR, result), HOUR)

    def test_no_last_run_uses_full_interval(self):
        self.assertEqual(
            _effective_interval('sync_external_parties_codes', HOUR, None), HOUR
        )


class ScheduleRegistrationTests(SimpleTestCase):
    """Задача накопления резерва зарегистрирована в расписании."""

    def test_accumulate_reserve_task_in_schedule(self):
        from app_scheduler.management.commands.run_scheduler import SCHEDULE

        names = {name for name, _, _ in SCHEDULE}
        self.assertIn('accumulate_short_shelf_life_reserve', names)

    def test_accumulate_reserve_task_exists(self):
        from app_scheduler.tasks import accumulate_short_shelf_life_reserve_task

        self.assertTrue(hasattr(accumulate_short_shelf_life_reserve_task, 'enqueue'))

    def test_sync_parties_task_in_schedule(self):
        from datetime import timedelta

        from app_scheduler.management.commands.run_scheduler import SCHEDULE
        from app_scheduler.tasks import sync_parties_task

        entries = {name: interval for name, interval, _ in SCHEDULE}
        self.assertIn('sync_parties', entries)
        self.assertEqual(entries['sync_parties'], timedelta(minutes=30))
        self.assertTrue(hasattr(sync_parties_task, 'enqueue'))

    def test_system_health_task_in_schedule(self):
        from datetime import timedelta

        from app_scheduler.management.commands.run_scheduler import SCHEDULE
        from app_scheduler.tasks import check_system_health_task

        entries = {name: interval for name, interval, _ in SCHEDULE}
        self.assertIn('check_system_health', entries)
        self.assertEqual(entries['check_system_health'], timedelta(minutes=5))
        self.assertTrue(hasattr(check_system_health_task, 'enqueue'))


class SchedulerRegistryTests(SimpleTestCase):
    """Единый реестр задач покрывает всё расписание SCHEDULE."""

    def test_registry_covers_schedule(self):
        from app_scheduler.registry import get_scheduled_tasks

        schedule_names = {name for name, _, _ in SCHEDULE}
        registry_names = set(get_scheduled_tasks().keys())
        self.assertEqual(registry_names, schedule_names)


class SchedulerStatusViewTests(TestCase):
    """API расписания отдаёт все задачи из SCHEDULE (только админ)."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='root', password='pass', email='root@example.com',
        )

    def test_requires_admin(self):
        user = User.objects.create_user(username='user', password='pass')
        self.client.force_login(user)

        response = self.client.get(reverse('status'))

        self.assertEqual(response.status_code, 403)

    def test_returns_all_scheduled_tasks(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('status'))

        self.assertEqual(response.status_code, 200)
        names = {task['name'] for task in response.json()['schedule']}
        self.assertEqual(names, {name for name, _, _ in SCHEDULE})
        # В каждой задаче есть признаки выполнения и прогресс.
        for task in response.json()['schedule']:
            self.assertIn('is_running', task)
            self.assertIn('is_queued', task)
            self.assertIn('progress', task)


class TaskProgressTests(TestCase):
    """Запись/чтение/очистка прогресса выполнения задачи."""

    def test_set_get_clear(self):
        from app_scheduler.progress import (
            clear_task_progress,
            get_task_progress,
            set_task_progress,
        )

        set_task_progress(
            'demo', phase='Этап 1', current=2, total=5, message='обработка',
        )

        progress = get_task_progress('demo')
        self.assertEqual(progress['phase'], 'Этап 1')
        self.assertEqual(progress['current'], 2)
        self.assertEqual(progress['total'], 5)
        self.assertEqual(progress['percent'], 40)
        self.assertEqual(progress['message'], 'обработка')

        clear_task_progress('demo')
        self.assertIsNone(get_task_progress('demo'))

    def test_missing_progress_returns_none(self):
        from app_scheduler.progress import get_task_progress

        self.assertIsNone(get_task_progress('нет-такой'))

    def test_percent_none_without_total(self):
        from app_scheduler.progress import set_task_progress, get_task_progress

        set_task_progress('demo2', phase='Без счётчика', current=3)

        self.assertIsNone(get_task_progress('demo2')['percent'])


class SchedulerRunViewTests(TestCase):
    """Ручной запуск задачи: только админ, ставит задачу в очередь."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='root', password='pass', email='root@example.com',
        )

    def test_requires_admin(self):
        user = User.objects.create_user(username='user', password='pass')
        self.client.force_login(user)

        response = self.client.post(reverse('run', args=['refresh_suz_token']))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()['is_error'])

    def test_unknown_task_returns_404(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse('run', args=['does-not-exist']))

        self.assertEqual(response.status_code, 404)

    def test_admin_enqueues_task(self):
        from django_tasks_db.models import DBTaskResult

        self.client.force_login(self.admin)
        before = DBTaskResult.objects.count()

        response = self.client.post(reverse('run', args=['refresh_suz_token']))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_error'])
        self.assertEqual(DBTaskResult.objects.count(), before + 1)

    def test_duplicate_pending_returns_409(self):
        self.client.force_login(self.admin)

        first = self.client.post(reverse('run', args=['refresh_suz_token']))
        second = self.client.post(reverse('run', args=['refresh_suz_token']))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertTrue(second.json()['is_error'])
