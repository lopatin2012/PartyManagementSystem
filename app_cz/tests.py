# app_cz/tests.py

"""
Тесты устойчивости синхронизации с внешним сервисом «Молвест.Маркировка».

Проверяют персональную для завода водяную метку (changed_since):
- успешная выгрузка двигает метку;
- сбой сети/ответа НЕ двигает метку (окно повторится);
- ошибки обработки заданий удерживают метку;
- сбой одного завода не влияет на другие.
"""

from datetime import date, timedelta
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from app_factory.models import (
    CardStateChoices,
    Factory,
    PackagingLevelChoices,
    Product,
    ProductGroupChoices,
    ProductPackaging,
    ProductSKU,
    StateConditionChoices,
    TypeFormationUIP,
)
from app_uip.models import PartyStatusChoices, ProductionParty, UIP
from app_cz.models import CISCode
from app_cz.services import code_sync
from app_cz.services import party_service


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


def _create_sku():
    product = Product.objects.create(
        group=ProductGroupChoices.MILK,
        name='Тестовый продукт',
        shelf_life_in_days=14,
        item_condition=StateConditionChoices.READY_ORDER_KM,
        card_status=CardStateChoices.PUBLISHED,
    )
    ProductPackaging.objects.create(
        product=product,
        level=PackagingLevelChoices.UNIT,
        gtin='04601751026019',
        quantity_inside=1,
    )
    return ProductSKU.objects.create(product=product, article='50032')


class SyncPartiesReserveReconciliationTests(TestCase):
    """Сверка резерва: локальные reserved_*, отсутствующие в ЧЗ → is_desync."""

    def setUp(self):
        self.sku = _create_sku()
        self.number_in_cz = '04601751026019260101500320000000'
        self.number_not_in_cz = '04601751026019260101500320000001'

    def _uip(self, number, status=PartyStatusChoices.RESERVED_LOCAL,
             is_desync=False):
        return UIP.objects.create(
            product_sku=self.sku,
            number=number,
            status=status,
            is_desync=is_desync,
        )

    def _sync(self, cz_parties, cises_statuses=None):
        with patch.object(
            party_service, 'get_all_reserved_parties',
            return_value={
                'is_error': False,
                'message_error': 'ОК',
                'lst_party_number_info': cz_parties,
            },
        ), patch(
            'app_cz.services.code_status.get_cises_statuses',
            return_value=cises_statuses or {},
        ):
            return party_service.sync_parties_from_cz()

    def test_local_reserved_missing_in_cz_marked_desync(self):
        uip_in = self._uip(self.number_in_cz)
        uip_missing = self._uip(self.number_not_in_cz)

        result = self._sync([{
            'partyNumber': self.number_in_cz,
            'gtin': '04601751026019',
        }])

        self.assertFalse(result['is_error'])
        self.assertEqual(result['desync'], 1)
        uip_in.refresh_from_db()
        uip_missing.refresh_from_db()
        self.assertFalse(uip_in.is_desync)
        self.assertTrue(uip_missing.is_desync)
        # Нет кодов → статус НЕ меняется, только рассинхрон.
        self.assertEqual(uip_missing.status, PartyStatusChoices.RESERVED_LOCAL)

    def test_missing_in_cz_with_applied_code_registers_uip(self):
        """Код УИП нанесён в ЧЗ → УИП регистрируется, а не помечается рассинхроном."""
        uip = self._uip(self.number_not_in_cz)
        party = ProductionParty.objects.create(
            uip=uip, production_party='1', external_number_task='task-1',
        )
        CISCode.objects.create(
            production_party=party,
            product_packaging=self.sku.product.packagings.first(),
            code='010460175102601921CODE0001',
            level=PackagingLevelChoices.UNIT,
        )

        result = self._sync(
            [],
            cises_statuses={'010460175102601921CODE0001': 'APPLIED'},
        )

        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.REGISTERED)
        self.assertFalse(uip.is_desync)
        self.assertEqual(result['desync'], 1)

    def test_desync_flag_cleared_when_back_in_cz(self):
        uip = self._uip(
            self.number_in_cz,
            status=PartyStatusChoices.RESERVED_LOCAL,
            is_desync=True,
        )

        result = self._sync([{
            'partyNumber': self.number_in_cz,
            'gtin': '04601751026019',
        }])

        uip.refresh_from_db()
        self.assertFalse(uip.is_desync)
        self.assertEqual(result['desync'], 0)

    def test_empty_cz_marks_all_local_reserved(self):
        self._uip(self.number_in_cz)
        self._uip(self.number_not_in_cz)

        result = self._sync([])

        self.assertEqual(result['desync'], 2)
        self.assertTrue(
            all(UIP.objects.values_list('is_desync', flat=True))
        )


class SyncPartiesFormatDetectionTests(TestCase):
    """Пункт 2: формат УИП из ЧЗ (локальный vs сгенерированный ЧЗ)."""

    def setUp(self):
        self.sku = _create_sku()
        from app_cz.services.party_service import build_local_party_number
        self.local_number = build_local_party_number(
            '04601751026019',
            date(2026, 1, 15),
            article='50032',
            party='000',
            type_formation_uip=TypeFormationUIP.general.value,
        )

    def _sync(self, cz_parties):
        with patch.object(
            party_service, 'get_all_reserved_parties',
            return_value={
                'is_error': False,
                'message_error': 'ОК',
                'lst_party_number_info': cz_parties,
            },
        ), patch(
            'app_cz.services.code_status.get_cises_statuses',
            return_value={},
        ):
            return party_service.sync_parties_from_cz()

    def test_local_format_number_created_as_reserved_local(self):
        result = self._sync([{
            'partyNumber': self.local_number,
            'gtin': '04601751026019',
            'productionDate': '2026-01-15',
        }])

        self.assertEqual(result['created'], 1)
        uip = UIP.objects.get(number=self.local_number)
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_LOCAL)

    def test_cz_format_number_created_as_reserved_cz(self):
        result = self._sync([{
            'partyNumber': '0460175102601926011520AB12XYZ999',
            'gtin': '04601751026019',
            'productionDate': '2026-01-15',
        }])

        self.assertEqual(result['created'], 1)
        uip = UIP.objects.get(number='0460175102601926011520AB12XYZ999')
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_CZ)


class GenerateUipManualEndpointTests(TestCase):
    """POST /cz/uip/generate/ в режиме mode=manual."""

    def setUp(self):
        self.sku = _create_sku()
        self.url = '/cz/uip/generate/'
        self.admin = User.objects.create_superuser(
            username='admin', password='pass', email='a@a.a'
        )

    def test_manual_generate_creates_reserved_uip(self):
        self.client.force_login(self.admin)
        with patch(
            'app_cz.services.party_service.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False, 'message_error': 'ОК',
                'lst_party_number_info': [],
            }
            response = self.client.post(
                self.url,
                {
                    'product_sku_id': str(self.sku.id),
                    'production_date': '2026-01-15',
                    'mode': 'manual',
                    'party_number': 'ABC123456789',
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        number = '04601751026019' + '260115' + 'ABC123456789'
        self.assertTrue(UIP.objects.filter(number=number).exists())

    def test_manual_generate_wrong_length_returns_400(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {
                'product_sku_id': str(self.sku.id),
                'production_date': '2026-01-15',
                'mode': 'manual',
                'party_number': 'A',
            },
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


class CheckUipNumberEndpointTests(TestCase):
    """GET /cz/uip/check-number/ — проверка номера в СУП и ЧЗ."""

    def setUp(self):
        self.sku = _create_sku()
        self.url = '/cz/uip/check-number/'
        self.admin = User.objects.create_superuser(
            username='admin', password='pass', email='a@a.a'
        )
        self.number = '04601751026019260101500320000000'

    def test_in_sup(self):
        self.client.force_login(self.admin)
        UIP.objects.create(
            product_sku=self.sku, number=self.number,
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        with patch(
            'app_cz.views.get_all_reserved_parties',
            return_value={'is_error': False, 'lst_party_number_info': []},
        ):
            response = self.client.get(self.url, {'number': self.number})
        data = response.json()
        self.assertTrue(data['in_sup'])
        self.assertFalse(data['in_cz'])

    def test_in_cz(self):
        self.client.force_login(self.admin)
        with patch(
            'app_cz.views.get_all_reserved_parties',
            return_value={
                'is_error': False,
                'lst_party_number_info': [{'partyNumber': self.number}],
            },
        ):
            response = self.client.get(
                self.url, {'number': self.number, 'check_cz': '1'},
            )
        data = response.json()
        self.assertFalse(data['in_sup'])
        self.assertTrue(data['in_cz'])

    def test_local_check_does_not_call_cz(self):
        self.client.force_login(self.admin)
        with patch('app_cz.views.get_all_reserved_parties') as mock_cz:
            response = self.client.get(self.url, {'number': self.number})
        data = response.json()
        self.assertFalse(data['in_sup'])
        self.assertFalse(data['in_cz'])
        mock_cz.assert_not_called()

    def test_cz_unavailable_does_not_crash(self):
        self.client.force_login(self.admin)
        with patch(
            'app_cz.views.get_all_reserved_parties',
            side_effect=RuntimeError('нет связи с сервисом подписей'),
        ):
            response = self.client.get(
                self.url, {'number': self.number, 'check_cz': '1'},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['in_cz'])
        self.assertTrue(data['cz_unavailable'])


class ReportUipEndpointTests(TestCase):
    """Пункт 4: кнопка/эндпоинт отправки отчёта о нанесении."""

    def setUp(self):
        self.sku = _create_sku()
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.url = '/cz/api/report-uip/'

    def _login(self, superuser=True):
        user = User.objects.create_superuser(
            username='admin', password='pass', email='a@a.a'
        )
        self.client.force_login(user)
        return user

    def test_requires_admin(self):
        response = self.client.post(
            self.url, data={'uip_id': str(self.uip.id)},
            content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))

    def test_no_task_returns_400(self):
        self._login()
        response = self.client.post(
            self.url, data={'uip_id': str(self.uip.id)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_successful_report_registers_uip(self):
        self._login()
        ProductionParty.objects.create(
            uip=self.uip, production_party='1', external_number_task='task-1',
            expiration_datetime=timezone.now(),
        )
        self.uip.production_date = date(2026, 1, 15)
        self.uip.save(update_fields=['production_date'])

        with patch(
            'app_cz.services.reserve_monitor.send_application_report',
            return_value={'has_error': False, 'status_close': True, 'responses': []},
        ), patch(
            'app_cz.services.reserve_monitor._fetch_code_for_task',
            return_value='010460175102601921CODE0001',
        ):
            response = self.client.post(
                self.url, data={'uip_id': str(self.uip.id)},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        self.uip.refresh_from_db()
        self.assertEqual(self.uip.status, PartyStatusChoices.REGISTERED)


class RegisterUipMarkingDateTests(TestCase):
    """Дата маркировки в отчёте не может быть в будущем (min(production_date, today))."""

    def setUp(self):
        self.sku = _create_sku()
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        ProductionParty.objects.create(
            uip=self.uip, production_party='1', external_number_task='task-1',
        )

    def _register(self, production_date):
        from app_cz.services.reserve_monitor import register_uip

        self.uip.production_date = production_date
        self.uip.save(update_fields=['production_date'])
        with patch(
            'app_cz.services.reserve_monitor.send_application_report',
            return_value={'has_error': False, 'status_close': True, 'responses': []},
        ) as mock_report, patch(
            'app_cz.services.reserve_monitor._fetch_code_for_task',
            return_value='010460175102601921CODE0001',
        ):
            register_uip(self.uip)
        return mock_report.call_args.kwargs

    def test_future_production_date_clamped_to_today(self):
        future = timezone.now().date() + timedelta(days=5)
        kwargs = self._register(future)
        self.assertEqual(kwargs['marking_date'], timezone.now().date().isoformat())
        # Срок годности задания нет → fallback = ограниченная дата.
        self.assertEqual(kwargs['exp_date'], timezone.now().date().isoformat())

    def test_past_production_date_kept(self):
        past = timezone.now().date() - timedelta(days=5)
        kwargs = self._register(past)
        self.assertEqual(kwargs['marking_date'], past.isoformat())
