# app_scheduler/tests.py

"""Тесты логики планировщика периодических задач."""

from django.test import SimpleTestCase
from django_tasks.base import TaskResultStatus

from app_scheduler.management.commands.run_scheduler import (
    FAST_RETRY_INTERVAL,
    HOUR,
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
