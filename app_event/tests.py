# app_event/tests.py

"""Тесты наблюдаемости: health-проверки, антидребезг, рассылка, очистка."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from app_event.models import HealthCheck, NotificationRecipient
from app_event.services import health as health_service

User = get_user_model()


def _fake_checks(ok: bool):
    """Набор результатов диагностики для подмены _diagnose."""
    return {
        'signatures': {
            'name': 'Сервис подписей',
            'ok': ok,
            'message': 'ОК' if ok else 'нет связи',
        },
    }


class HealthCheckTests(TestCase):
    def setUp(self):
        self.group, _ = Group.objects.get_or_create(name='Мониторинг')
        NotificationRecipient.objects.get_or_create(group=self.group)
        self.monitor = User.objects.create_user(
            username='monitor', password='pass', email='monitor@test.local',
        )
        self.monitor.groups.add(self.group)

    def test_first_run_does_not_alert_until_confirmed(self):
        """Первый сбой не шлёт письмо — ждём подтверждения (порог)."""
        with patch.object(
            health_service, '_diagnose', return_value=_fake_checks(False)
        ), patch.object(
            health_service, '_send_alert'
        ) as mock_alert:
            result = health_service.run_health_checks()

        self.assertEqual(HealthCheck.objects.filter(service='signatures').count(), 1)
        mock_alert.assert_not_called()
        self.assertEqual(result['failed'], [])

    def test_alert_after_confirmed_attempts(self):
        """Письмо о сбое — только когда серия неудач достигла порога."""
        with patch.object(health_service, '_confirm_attempts', return_value=3):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                health_service.run_health_checks()  # 1
                health_service.run_health_checks()  # 2
                self.assertEqual(mock_alert.call_count, 0)

                result = health_service.run_health_checks()  # 3 — подтверждено

        mock_alert.assert_called_once()
        self.assertIn('СБОЙ', mock_alert.call_args[0][0])
        self.assertIn('signatures', result['failed'])

    def test_no_duplicate_alert_while_still_failing(self):
        """Повторные неудачи после подтверждения письма не шлют."""
        with patch.object(health_service, '_confirm_attempts', return_value=2):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                health_service.run_health_checks()  # 1
                health_service.run_health_checks()  # 2 — письмо
                health_service.run_health_checks()  # 3 — без письма
                health_service.run_health_checks()  # 4 — без письма

        mock_alert.assert_called_once()

    def test_transient_failure_does_not_alert(self):
        """Одиночное «мигание» (успех после сбоя до порога) не шлёт писем."""
        with patch.object(health_service, '_confirm_attempts', return_value=3):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                health_service.run_health_checks()  # 1 сбой

            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(True)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                health_service.run_health_checks()  # успех

        mock_alert.assert_not_called()

    def test_recovery_includes_failure_details(self):
        """В письме о восстановлении — число сбоев и оформленный HTML."""
        with patch.object(health_service, '_confirm_attempts', return_value=2):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert'):
                health_service.run_health_checks()
                health_service.run_health_checks()  # подтверждённый сбой

            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(True)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                result = health_service.run_health_checks()  # восстановление

        self.assertIn('signatures', result['failed'])
        mock_alert.assert_called_once()
        subject, body = mock_alert.call_args[0][0], mock_alert.call_args[0][1]
        self.assertIn('ВОССТАНОВЛЕНО', subject)
        self.assertIn('Время восстановления', body)
        self.assertIn('Сбоев подряд перед восстановлением', body)
        # HTML передан третьим аргументом (html_message).
        html = mock_alert.call_args.kwargs.get('html_message')
        self.assertIn('<div', html)

    def test_no_alert_on_recovery_without_confirmed_failure(self):
        """Если подтверждённого сбоя не было — о восстановлении не пишем."""
        with patch.object(health_service, '_confirm_attempts', return_value=3):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert'):
                health_service.run_health_checks()  # 1 сбой (не подтверждён)

            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(True)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                result = health_service.run_health_checks()  # успех

        mock_alert.assert_not_called()
        self.assertEqual(result['failed'], [])

    def test_alert_on_recovery(self):
        with patch.object(health_service, '_confirm_attempts', return_value=2):
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(False)
            ), patch.object(health_service, '_send_alert'):
                health_service.run_health_checks()
                health_service.run_health_checks()
            with patch.object(
                health_service, '_diagnose', return_value=_fake_checks(True)
            ), patch.object(health_service, '_send_alert') as mock_alert:
                result = health_service.run_health_checks()

        self.assertIn('signatures', result['failed'])
        mock_alert.assert_called_once()
        self.assertIn('ВОССТАНОВЛЕНО', mock_alert.call_args[0][0])

    def test_get_alert_recipients_from_group_users(self):
        # Пользователь без email не попадает.
        no_email = User.objects.create_user(
            username='noemail', password='pass', email='',
        )
        no_email.groups.add(self.group)

        recipients = health_service.get_alert_recipients()
        self.assertEqual(recipients, ['monitor@test.local'])

    def test_recipients_empty_when_inactive_recipient(self):
        NotificationRecipient.objects.filter(group=self.group).update(is_active=False)
        self.assertEqual(health_service.get_alert_recipients(), [])

    def test_recipients_empty_when_no_group_users(self):
        NotificationRecipient.objects.all().delete()
        self.assertEqual(health_service.get_alert_recipients(), [])

    def test_cleanup_removes_old_checks(self):
        old = HealthCheck.objects.create(
            service='signatures', name='Сервис подписей', is_ok=True,
        )
        HealthCheck.objects.filter(id=old.id).update(
            checked_at=timezone.now() - timedelta(days=40)
        )
        HealthCheck.objects.create(
            service='signatures', name='Сервис подписей', is_ok=True,
        )

        result = health_service.cleanup_old_health_checks()
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(HealthCheck.objects.count(), 1)


class HealthEndpointTests(TestCase):
    """Публичный GET /health/."""

    def test_ok_returns_200(self):
        with patch(
            'app_helper.views.diagnose_service',
            return_value={'is_available': True, 'checks': {}},
        ):
            response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    def test_fail_returns_503(self):
        with patch(
            'app_helper.views.diagnose_service',
            return_value={'is_available': False, 'checks': {'suz': {'ok': False}}},
        ):
            response = self.client.get('/health/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'fail')
