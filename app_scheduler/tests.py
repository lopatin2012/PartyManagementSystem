# app_scheduler/tests.py

"""Тесты логики планировщика периодических задач."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks.base import TaskResultStatus

from app_scheduler.management.commands.run_scheduler import (
    FAST_RETRY_INTERVAL,
    FIXED_DAILY_TIMES,
    HOUR,
    SCHEDULE,
    effective_interval,
    is_fixed_daily_due,
    next_fixed_daily_run,
)


class _FakeResult:
    """Минимальная заглушка DBTaskResult для проверки статуса."""

    def __init__(self, status):
        self.status = status


class _FakeRun:
    """Заглушка последнего запуска задачи (только finished_at)."""

    def __init__(self, finished_at):
        self.finished_at = finished_at


class EffectiveIntervalTests(SimpleTestCase):
    """Упавшие задачи из FAST_RETRY_TASKS повторяются раньше."""

    def test_failed_external_sync_retries_fast(self):
        result = _FakeResult(TaskResultStatus.FAILED)
        self.assertEqual(
            effective_interval('sync_external_parties_codes', HOUR, result),
            FAST_RETRY_INTERVAL,
        )

    def test_successful_external_sync_uses_full_interval(self):
        result = _FakeResult(TaskResultStatus.SUCCESSFUL)
        self.assertEqual(
            effective_interval('sync_external_parties_codes', HOUR, result),
            HOUR,
        )

    def test_other_tasks_are_not_fast_retried(self):
        result = _FakeResult(TaskResultStatus.FAILED)
        self.assertEqual(effective_interval('refresh_suz_token', HOUR, result), HOUR)

    def test_no_last_run_uses_full_interval(self):
        self.assertEqual(
            effective_interval('sync_external_parties_codes', HOUR, None), HOUR
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

    def test_resolve_desync_task_in_schedule(self):
        from datetime import timedelta

        from app_scheduler.management.commands.run_scheduler import SCHEDULE
        from app_scheduler.tasks import resolve_desync_uips_task

        entries = {name: interval for name, interval, _ in SCHEDULE}
        self.assertIn('resolve_desync_uips', entries)
        self.assertEqual(entries['resolve_desync_uips'], timedelta(hours=1))
        self.assertTrue(hasattr(resolve_desync_uips_task, 'enqueue'))

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


class FixedDailyScheduleTests(SimpleTestCase):
    """Накопление резерва запускается в фиксированное время (00:30)."""

    TASK = 'accumulate_short_shelf_life_reserve'

    def _at(self, hour, minute):
        from django.utils import timezone

        return timezone.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )

    def test_task_scheduled_at_0030(self):
        self.assertEqual(FIXED_DAILY_TIMES[self.TASK], (0, 30))

    def test_not_due_before_time(self):
        now = self._at(0, 0)
        self.assertFalse(is_fixed_daily_due(self.TASK, None, now))

    def test_due_after_time_without_run(self):
        now = self._at(0, 45)
        self.assertTrue(is_fixed_daily_due(self.TASK, None, now))

    def test_not_due_again_after_running_today(self):
        now = self._at(0, 45)
        last = _FakeRun(self._at(0, 31))
        self.assertFalse(is_fixed_daily_due(self.TASK, last, now))

    def test_due_again_next_day(self):
        from datetime import timedelta

        now = self._at(0, 45)
        last = _FakeRun(self._at(0, 31) - timedelta(days=1))
        self.assertTrue(is_fixed_daily_due(self.TASK, last, now))

    def test_next_run_is_tomorrow_after_running_today(self):
        from datetime import timedelta

        now = self._at(0, 45)
        last = _FakeRun(self._at(0, 31))
        self.assertEqual(
            next_fixed_daily_run(self.TASK, last, now),
            self._at(0, 30) + timedelta(days=1),
        )


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

    def test_accumulate_task_shows_fixed_time(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('status'))

        tasks = {task['name']: task for task in response.json()['schedule']}
        self.assertEqual(
            tasks['accumulate_short_shelf_life_reserve']['interval_display'],
            'ежедневно 00:30',
        )


class SchedulerCheckEnqueueTests(TestCase):
    """_check_and_enqueue не падает в ветке «ещё рано» (регресс)."""

    def test_not_due_branch_does_not_crash(self):
        import io

        from django.utils import timezone
        from django_tasks_db.models import DBTaskResult

        from app_scheduler.management.commands.run_scheduler import Command
        from app_scheduler.registry import get_scheduled_tasks

        task_map = get_scheduled_tasks()
        task_func = task_map['refresh_suz_token']
        ref = task_func.func
        task_path = f'{ref.__module__}.{ref.__name__}'

        # Свежий успешный запуск: интервал (6 ч) ещё не истёк.
        task_func.enqueue()
        DBTaskResult.objects.filter(task_path=task_path).update(
            status=TaskResultStatus.SUCCESSFUL,
            finished_at=timezone.now(),
        )

        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        # Не должно бросать AttributeError (интервал — timedelta, не функция).
        command._check_and_enqueue(task_map, DBTaskResult, TaskResultStatus)


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


class CleanupStaleDraftsTests(TestCase):
    """Устаревшие черновики УИП переводятся в «Удалён»."""

    def setUp(self):
        from app_factory.models import (
            CardStateChoices,
            Product,
            ProductGroupChoices,
            ProductSKU,
            StateConditionChoices,
        )

        self.today = timezone.now().date()
        product = Product.objects.create(
            group=ProductGroupChoices.MILK,
            name='Тестовый продукт',
            shelf_life_in_days=14,
            item_condition=StateConditionChoices.READY_ORDER_KM,
            card_status=CardStateChoices.PUBLISHED,
        )
        self.sku = ProductSKU.objects.create(
            product=product, article='50032',
        )

    def _uip(self, production_date, status=None, number=None):
        from app_cz.services.party_service import build_local_party_number
        from app_uip.models import PartyStatusChoices, UIP

        if number is None:
            number = build_local_party_number(
                '04601751026019',
                production_date,
                article='50032',
                party='000',
                type_formation_uip=1,
            )
        return UIP.objects.create(
            product_sku=self.sku,
            number=number,
            status=status or PartyStatusChoices.DRAFT,
            production_date=production_date,
        )

    def test_stale_draft_is_deleted(self):
        from app_scheduler.tasks import cleanup_stale_drafts_task
        from app_uip.models import PartyStatusChoices, UIPStatusLog

        stale = self._uip(self.today - timedelta(days=31))
        fresh = self._uip(self.today - timedelta(days=29))
        no_date = self._uip(
            None, number='04601751026019260101000000000000',
        )
        reserved = self._uip(
            self.today - timedelta(days=60),
            status=PartyStatusChoices.RESERVED_LOCAL,
        )

        result = cleanup_stale_drafts_task.func()

        self.assertEqual(result['deleted'], 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        no_date.refresh_from_db()
        reserved.refresh_from_db()
        self.assertEqual(stale.status, PartyStatusChoices.DELETED)
        self.assertEqual(fresh.status, PartyStatusChoices.DRAFT)
        self.assertEqual(no_date.status, PartyStatusChoices.DRAFT)
        self.assertEqual(reserved.status, PartyStatusChoices.RESERVED_LOCAL)
        log = UIPStatusLog.objects.get(uip=stale)
        self.assertEqual(log.from_status, PartyStatusChoices.DRAFT)
        self.assertEqual(log.to_status, PartyStatusChoices.DELETED)
        self.assertIn('устаревшего черновика', log.note)

    @override_settings(UIP_DRAFT_RETENTION_DAYS=0)
    def test_zero_days_disables_cleanup(self):
        from app_scheduler.tasks import cleanup_stale_drafts_task
        from app_uip.models import PartyStatusChoices

        stale = self._uip(self.today - timedelta(days=365))

        result = cleanup_stale_drafts_task.func()

        self.assertEqual(result['deleted'], 0)
        stale.refresh_from_db()
        self.assertEqual(stale.status, PartyStatusChoices.DRAFT)
