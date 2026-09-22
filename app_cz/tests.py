# app_cz/tests.py

"""
Тесты устойчивости синхронизации с внешним сервисом «Молвест.Маркировка».

Проверяют персональную для завода водяную метку (changed_since):
- успешная выгрузка двигает метку;
- сбой сети/ответа НЕ двигает метку (окно повторится);
- ошибки обработки заданий удерживают метку;
- сбой одного завода не влияет на другие.
"""

from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TestCase
from django.utils import timezone

from app_factory.models import Factory
from app_cz.services import code_sync


class FetchExternalTasksChangedSinceTests(TestCase):
    """_fetch_external_tasks_changed_since: None при сбое, список при успехе."""

    def test_returns_none_on_network_error(self):
        with patch(
            'app_cz.services.code_sync.requests.get',
            side_effect=code_sync.requests.exceptions.ConnectionError('boom'),
        ):
            result = code_sync._fetch_external_tasks_changed_since(
                'http://127.0.0.1:8000', timezone.now()
            )
        self.assertIsNone(result)

    def test_returns_none_on_error_payload(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'is_error': True, 'message': 'недоступно'}
        with patch('app_cz.services.code_sync.requests.get', return_value=response):
            result = code_sync._fetch_external_tasks_changed_since(
                'http://127.0.0.1:8000', timezone.now()
            )
        self.assertIsNone(result)

    def test_returns_list_on_success(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'is_error': False, 'count': 1, 'result': [{'uuid_str': 'abc'}],
        }
        with patch('app_cz.services.code_sync.requests.get', return_value=response):
            result = code_sync._fetch_external_tasks_changed_since(
                'http://127.0.0.1:8000', timezone.now()
            )
        self.assertEqual(result, [{'uuid_str': 'abc'}])

    def test_returns_empty_list_when_no_changes(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'is_error': False, 'count': 0, 'result': []}
        with patch('app_cz.services.code_sync.requests.get', return_value=response):
            result = code_sync._fetch_external_tasks_changed_since(
                'http://127.0.0.1:8000', timezone.now()
            )
        self.assertEqual(result, [])


class SyncExternalPartiesWatermarkTests(TestCase):
    """Персональная водяная метка завода и её поведение при сбоях."""

    def setUp(self):
        self.factory = Factory.objects.create(
            name='Тестовый завод',
            ip_address='127.0.0.1',
            port_address=8000,
            is_active=True,
        )

    def _sync(self, fetch_result, receive_result=None):
        receive_result = receive_result or {
            'has_error': False, 'created': True, 'message': 'ok',
        }
        with patch.object(
            code_sync, '_fetch_external_tasks_changed_since',
            return_value=fetch_result,
        ), patch.object(
            code_sync, 'receive_external_task', return_value=receive_result,
        ), patch.object(
            code_sync, 'sync_all_external_tasks',
            return_value={'is_error': False, 'message': ''},
        ):
            return code_sync.sync_external_parties_and_codes(
                task_path='app_scheduler.tasks.sync_external_parties_codes_task'
            )

    def test_success_advances_watermark(self):
        before = timezone.now()
        result = self._sync([{'uuid_str': 'a'}])
        self.factory.refresh_from_db()

        self.assertFalse(result['is_error'])
        self.assertIsNotNone(self.factory.external_sync_changed_since)
        # Метка сохраняется «назад» на безопасный зазор, но не в будущее.
        self.assertLessEqual(self.factory.external_sync_changed_since, before)
        self.assertIsNotNone(self.factory.external_sync_success_at)
        self.assertEqual(self.factory.external_sync_error, '')

    def test_failure_does_not_advance_watermark(self):
        self._sync([])
        self.factory.refresh_from_db()
        first = self.factory.external_sync_changed_since
        self.assertIsNotNone(first)

        result = self._sync(None)
        self.factory.refresh_from_db()

        self.assertTrue(result['is_error'])
        self.assertEqual(self.factory.external_sync_changed_since, first)
        self.assertIn('Не удалось выгрузить', self.factory.external_sync_error)
        self.assertIn(self.factory.name, result['failed_factories'])

    def test_task_processing_errors_hold_watermark(self):
        self._sync([])
        self.factory.refresh_from_db()
        first = self.factory.external_sync_changed_since

        result = self._sync(
            [{'uuid_str': 'a'}],
            receive_result={'has_error': True, 'message': 'db error'},
        )
        self.factory.refresh_from_db()

        self.assertTrue(result['is_error'])
        self.assertEqual(self.factory.external_sync_changed_since, first)
        self.assertIn(self.factory.name, result['failed_factories'])

    def test_no_factories_does_not_crash(self):
        Factory.objects.all().delete()
        result = code_sync.sync_external_parties_and_codes()
        self.assertFalse(result['is_error'])
        self.assertIn('Нет действующих заводов', result['message'])

    def test_per_factory_isolation(self):
        other = Factory.objects.create(
            name='Второй завод',
            ip_address='10.0.0.1',
            port_address=9000,
            is_active=True,
        )

        def fake_fetch(url, changed_since):
            return None if ':9000' in url else []

        with patch.object(
            code_sync, '_fetch_external_tasks_changed_since',
            side_effect=fake_fetch,
        ), patch.object(
            code_sync, 'sync_all_external_tasks',
            return_value={'is_error': False, 'message': ''},
        ):
            result = code_sync.sync_external_parties_and_codes()

        self.factory.refresh_from_db()
        other.refresh_from_db()

        self.assertIsNotNone(self.factory.external_sync_changed_since)
        self.assertIsNone(other.external_sync_changed_since)
        self.assertIn(other.name, result['failed_factories'])
        self.assertNotIn(self.factory.name, result['failed_factories'])


class GetFactoryChangedSinceTests(TestCase):
    """get_factory_changed_since: сохранённая метка имеет приоритет."""

    def setUp(self):
        self.factory = Factory.objects.create(
            name='Завод метки', ip_address='127.0.0.1', port_address=8001,
        )

    def test_uses_saved_watermark(self):
        ts = timezone.now() - timedelta(days=1)
        self.factory.external_sync_changed_since = ts
        self.assertEqual(code_sync.get_factory_changed_since(self.factory), ts)

    def test_falls_back_when_empty(self):
        with patch.object(
            code_sync, '_last_external_sync_changed_since',
            return_value='FALLBACK',
        ):
            self.assertEqual(
                code_sync.get_factory_changed_since(self.factory), 'FALLBACK'
            )
