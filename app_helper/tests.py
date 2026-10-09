from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from app_helper import load_tracker, service_helper


class LoadStatsMessageTests(SimpleTestCase):
    """Сводка нагрузки содержит числа и порог (для панели статусов)."""

    def test_high_load_message_has_numbers_and_threshold(self):
        high = load_tracker.HIGH_LOAD_THRESHOLD + 5_000
        with patch.object(
            load_tracker, 'get_requests_per_hour', return_value=high,
        ), patch.object(
            load_tracker, 'get_requests_per_minute', return_value=250,
        ):
            stats = load_tracker.get_load_stats()

        self.assertTrue(stats['is_high_load'])
        self.assertIn(str(high), stats['message'])
        self.assertIn('250', stats['message'])
        self.assertIn(str(load_tracker.HIGH_LOAD_THRESHOLD), stats['message'])

    def test_normal_load_message_has_numbers(self):
        with patch.object(
            load_tracker, 'get_requests_per_hour', return_value=100,
        ), patch.object(
            load_tracker, 'get_requests_per_minute', return_value=2,
        ):
            stats = load_tracker.get_load_stats()

        self.assertFalse(stats['is_high_load'])
        self.assertIn('100', stats['message'])


class SessionCookieNameTests(SimpleTestCase):
    """SESSION_COOKIE_NAME по умолчанию стабилен между перезапусками."""

    def test_default_name_is_stable_across_reload(self):
        import importlib
        import os

        import config.settings as settings_module

        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key != 'SESSION_COOKIE_NAME'
        }
        with patch.dict(os.environ, clean_env, clear=True), patch(
            'dotenv.load_dotenv',
        ):
            importlib.reload(settings_module)
            first = settings_module.SESSION_COOKIE_NAME
            importlib.reload(settings_module)
            second = settings_module.SESSION_COOKIE_NAME

        self.assertEqual(first, 'pms_sessionid')
        self.assertEqual(first, second)


class DiagnoseServiceTests(TestCase):
    """Самодиагностика: у проверок есть читаемое имя и понятное сообщение."""

    def _diagnose(self):
        with patch.object(
            service_helper, 'check_suz_token',
            return_value={'is_ok': True, 'message': 'Токен действителен'},
        ), patch.object(
            service_helper, 'check_signatures',
            return_value={'is_ok': True, 'message': 'HTTP 200'},
        ), patch.object(
            service_helper, 'check_factories',
            return_value={'is_ok': True, 'factories': []},
        ), patch.object(
            service_helper, 'get_load_stats',
            return_value={
                'requests_per_minute': 300,
                'requests_per_hour': 20000,
                'threshold_per_hour': 10000,
                'is_high_load': True,
                'message': (
                    'Высокая нагрузка: 20000 запросов/час '
                    '(порог 10000), за минуту 300'
                ),
            },
        ):
            return service_helper.diagnose_service()

    def test_load_check_has_readable_name_and_message(self):
        diagnosis = self._diagnose()

        load = diagnosis['checks']['load']
        self.assertEqual(load['name'], 'Нагрузка')
        self.assertFalse(load['ok'])
        self.assertIn('20000', load['message'])
        self.assertFalse(diagnosis['is_available'])

    def test_summary_lists_readable_names(self):
        diagnosis = self._diagnose()

        summary = diagnosis['checks']['summary']
        self.assertIn('Нагрузка', summary['message'])

    def test_every_check_has_name_and_message(self):
        diagnosis = self._diagnose()

        for key, check in diagnosis['checks'].items():
            self.assertTrue(check.get('name'), key)
            self.assertTrue(check.get('message'), key)
