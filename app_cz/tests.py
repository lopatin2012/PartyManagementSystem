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
from app_uip.models import (
    PartyStatusChoices,
    ProductionParty,
    ProductionPartyStatusChoices,
    ProductionPartySyncStatusChoices,
    UIP,
)
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


class SyncCodesErrorDetailsTests(TestCase):
    """sync_all_external_tasks собирает детали ошибок кодов."""

    def setUp(self):
        from app_factory.models import Line, Workshop

        self.factory = Factory.objects.create(
            name='Завод кодов', ip_address='127.0.0.1', port_address=8010,
        )
        workshop = Workshop.objects.create(factory=self.factory, name='Цех')
        self.line = Line.objects.create(workshop=workshop, name='Линия')
        self.sku = _create_sku()
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.party = ProductionParty.objects.create(
            uip=self.uip,
            line=self.line,
            production_party='1',
            external_number_task='task-1',
            is_external=True,
            status=ProductionPartyStatusChoices.WORK,
            external_created_at=timezone.now(),
        )

    def test_error_details_collected(self):
        with patch.object(
            code_sync, 'sync_codes_for_party',
            return_value={'has_error': True, 'message': 'сервер завода недоступен'},
        ):
            result = code_sync.sync_all_external_tasks()

        self.assertTrue(result['is_error'])
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['error_details'][0]['party'], 'task-1')
        self.assertEqual(result['error_details'][0]['factory'], 'Завод кодов')
        self.assertEqual(result['error_details'][0]['url'], 'http://127.0.0.1:8010')
        self.assertIn('сервер завода недоступен', result['error_details'][0]['message'])

    def test_fallback_packaging_when_unit_inactive(self):
        """Нет активной UNIT — берётся любая активная упаковка, ошибки нет."""
        from app_factory.models import ProductPackaging

        # Гасим UNIT-упаковку продукта, оставляем активную GROUP.
        ProductPackaging.objects.filter(
            product=self.sku.product, level=1,
        ).update(is_active=False)
        ProductPackaging.objects.create(
            product=self.sku.product,
            level=2,
            gtin='04601751026099',
            quantity_inside=6,
        )

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'is_error': False, 'sntins_camera': [], 'sntins_printer': [],
        }
        with patch('app_cz.services.code_sync.requests.get', return_value=response):
            result = code_sync.sync_codes_task(
                url='http://127.0.0.1:8010',
                task_id='task-1',
                production_party_id=str(self.party.id),
            )

        self.assertFalse(result.get('has_error'))
        self.party.refresh_from_db()
        self.assertNotIn('потребительская упаковка', self.party.last_sync_message or '')


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


class GenerateUipTypeSyncTests(TestCase):
    """Внешний generate-uip: расхождение типа УИП синхронизирует продукт в Молвест."""

    def setUp(self):
        self.sku = _create_sku()
        self.sku.type_formation_uip = TypeFormationUIP.party_end.value
        self.sku.save(update_fields=['type_formation_uip'])
        # Завод с IP тестового клиента — для поиска при синхронизации.
        Factory.objects.create(
            name='Тест-завод', ip_address='127.0.0.1', port_address=8000, is_active=True,
        )
        self.url = '/cz/api/v1/generate-uip/'

    def _post(self, type_value, party=''):
        body = {
            'gtin': self.sku.product.consumer_gtin,
            'production_date': '2026-01-15',
            'mode': 'local',
            'skip_cz': True,
            'type_formation_uip': type_value,
        }
        if party:
            body['party'] = party
        return self.client.post(self.url, body, content_type='application/json')

    def test_party_used_in_number(self):
        """Переданная партия попадает в номер для типов «с партией»."""
        response = self._post(int(self.sku.type_formation_uip), party='7')

        self.assertEqual(response.status_code, 200)
        # party_end: партия '7' добивается до 3 и идёт в конце.
        self.assertTrue(response.json()['number'].endswith('007'))

    def test_mismatch_pushes_sup_type(self):
        with patch(
            'app_factory.services.product_activity_sync.push_product_uip_type',
            return_value=True,
        ) as mock_push:
            response = self._post(TypeFormationUIP.general.value)

        self.assertEqual(response.status_code, 200)
        mock_push.assert_called_once()
        args = mock_push.call_args.args
        self.assertEqual(args[1], self.sku.article)
        self.assertEqual(int(args[2]), int(self.sku.type_formation_uip))

    def test_match_does_not_push(self):
        with patch(
            'app_factory.services.product_activity_sync.push_product_uip_type',
        ) as mock_push:
            response = self._post(int(self.sku.type_formation_uip))

        self.assertEqual(response.status_code, 200)
        mock_push.assert_not_called()

    def test_natura_valid_party_ok(self):
        """НатураПРО: 7 цифр партии → УИП 32 символа."""
        self.sku.type_formation_uip = TypeFormationUIP.natura.value
        self.sku.save(update_fields=['type_formation_uip'])

        response = self._post(int(self.sku.type_formation_uip), party='2635798')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['number'].endswith('-2635798'))

    def test_natura_short_party_rejected(self):
        """НатураПРО: партия не 7 цифр → УИП не 32 символа → 400."""
        self.sku.type_formation_uip = TypeFormationUIP.natura.value
        self.sku.save(update_fields=['type_formation_uip'])

        response = self._post(int(self.sku.type_formation_uip), party='123')

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


class BuildLocalPartyNumberNaturaTests(TestCase):
    """Локальный УИП формата «НатураПРО» (type_formation_uip=4)."""

    def _build(self, gtin, production_date, party):
        from app_cz.services.party_service import build_local_party_number

        return build_local_party_number(
            gtin,
            production_date,
            article='14362',
            party=party,
            type_formation_uip=TypeFormationUIP.natura.value,
        )

    def test_natura_format_matches_spec(self):
        # GTIN(14) + ГГММДД(6) + «0000-» + внутренний номер партии.
        number = self._build('04601751029980', date(2026, 7, 24), '2619520')

        self.assertEqual(number, '046017510299802607240000-2619520')
        self.assertEqual(len(number), 32)
        from app_cz.services.party_service import validate_party_number
        self.assertTrue(validate_party_number(number))

    def test_natura_strips_line_marker_suffix(self):
        # В графе «Партия» может быть маркер линии в конце («2635798g»).
        number = self._build('04601751024831', date(2026, 9, 29), '2635798g')

        self.assertEqual(number, '046017510248312609290000-2635798')

    def test_natura_full_number_from_report(self):
        # Полный номер из обращения: внутренний номер партии идёт в УИП
        # как есть (7 цифр), без авто-добавления год/неделя/день.
        number = self._build('04601751024930', date(2026, 10, 1), '2640501')

        self.assertEqual(number, '046017510249302610010000-2640501')
        self.assertEqual(len(number), 32)


class DeletedTaskSyncStatusTests(TestCase):
    """Удалённое задание считается синхронизированным («Ожидают» не висит)."""

    def test_receive_deleted_marks_synced(self):
        result = code_sync.receive_external_task({
            'uuid_str': 'task-deleted-1',
            'status': 'Удалено',
        })

        self.assertFalse(result['has_error'])
        party = ProductionParty.objects.get(external_number_task='task-deleted-1')
        self.assertEqual(party.status, ProductionPartyStatusChoices.DELETED)
        self.assertEqual(
            party.sync_status, ProductionPartySyncStatusChoices.SYNCED,
        )

    def test_receive_work_still_pending(self):
        code_sync.receive_external_task({
            'uuid_str': 'task-work-1',
            'status': 'В работе',
        })

        party = ProductionParty.objects.get(external_number_task='task-work-1')
        self.assertEqual(party.status, ProductionPartyStatusChoices.WORK)
        self.assertEqual(
            party.sync_status, ProductionPartySyncStatusChoices.PENDING,
        )

    def test_sync_all_marks_existing_deleted_synced(self):
        party = ProductionParty.objects.create(
            production_party='1',
            external_number_task='task-deleted-2',
            is_external=True,
            status=ProductionPartyStatusChoices.DELETED,
            sync_status=ProductionPartySyncStatusChoices.PENDING,
        )

        with patch.object(code_sync, 'sync_codes_for_party') as mock_sync:
            code_sync.sync_all_external_tasks()

        party.refresh_from_db()
        self.assertEqual(
            party.sync_status, ProductionPartySyncStatusChoices.SYNCED,
        )
        mock_sync.assert_not_called()


class SyncProgressReportingTests(TestCase):
    """Синхронизация пишет прогресс для окна «Фоновые задачи»."""

    def setUp(self):
        self.factory = Factory.objects.create(
            name='Завод прогресса', ip_address='127.0.0.1', port_address=8020,
        )
        ProductionParty.objects.create(
            production_party='1',
            external_number_task='task-progress-1',
            is_external=True,
            status=ProductionPartyStatusChoices.WORK,
            external_created_at=timezone.now(),
        )

    def test_sync_all_reports_codes_phase(self):
        with patch(
            'app_scheduler.progress.set_task_progress',
        ) as mock_progress, patch.object(
            code_sync, 'sync_codes_for_party',
            return_value={'has_error': False, 'synced_count': 0, 'updated_count': 0},
        ):
            code_sync.sync_all_external_tasks(
                progress_name='sync_external_parties_codes',
            )

        mock_progress.assert_called()
        first = mock_progress.call_args_list[0]
        self.assertEqual(first[0][0], 'sync_external_parties_codes')
        self.assertEqual(first[1]['phase'], 'Синхронизация кодов')
        self.assertEqual(first[1]['total'], 1)

    def test_sync_parties_reports_factory_phase(self):
        with patch(
            'app_scheduler.progress.set_task_progress',
        ) as mock_progress, patch.object(
            code_sync, '_fetch_external_tasks_changed_since', return_value=[],
        ), patch.object(
            code_sync, 'sync_all_external_tasks',
            return_value={'is_error': False, 'message': ''},
        ):
            code_sync.sync_external_parties_and_codes(
                progress_name='sync_external_parties_codes',
            )

        phases = [call[1].get('phase') for call in mock_progress.call_args_list]
        self.assertIn('Выгрузка заданий', phases)



class TrueApiSessionTokenTests(TestCase):
    """Получение токена TrueAPI: безопасный контракт (без исключений)."""

    def setUp(self):
        from app_cz.models import SUZAccount
        self.account = SUZAccount.objects.create(
            is_active=True,
            certificate_name='Тестовый сертификат',
            serial_number='0123456789ABCDEF',
            inn='7701234567',
            oms_id='oms-1',
            device_name='Устройство',
            connection_identifier='conn-1',
        )

    def _ok_response(self, payload):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        response.text = str(payload)
        return response

    @patch('app_cz.services.suz_client.attached_signed_data',
           return_value=('data', 'SIGNED'))
    @patch('app_cz.services.suz_client.get_true_api_auth_key',
           return_value={'uuid': 'u-1', 'data': 'data'})
    @patch('app_cz.services.suz_client.requests.post')
    def test_returns_uuid_token_when_united_token(self, mock_post, *_):
        """При unitedToken читаем uuidToken (как в «Молвест.Маркировка»)."""
        from app_cz.services.suz_client import get_true_api_session_token

        mock_post.return_value = self._ok_response({'uuidToken': 'TOKEN-UUID'})

        result = get_true_api_session_token()

        self.assertEqual(result['token'], 'TOKEN-UUID')
        self.assertTrue(result['uuid'])

    @patch('app_cz.services.suz_client.attached_signed_data',
           return_value=('data', 'SIGNED'))
    @patch('app_cz.services.suz_client.get_true_api_auth_key',
           return_value={'uuid': 'u-1', 'data': 'data'})
    @patch('app_cz.services.suz_client.requests.post')
    def test_falls_back_to_token_field(self, mock_post, *_):
        from app_cz.services.suz_client import get_true_api_session_token

        mock_post.return_value = self._ok_response({'token': 'TOKEN-PLAIN'})

        result = get_true_api_session_token()
        self.assertEqual(result['token'], 'TOKEN-PLAIN')

    @patch('app_cz.services.suz_client.get_true_api_auth_key',
           side_effect=Exception('Сервис Честного Знака не отвечает'))
    def test_auth_key_error_returns_no_token_not_raise(self, *_):
        """Сбой /auth/key не бросает — возвращает token=None и message."""
        from app_cz.services.suz_client import get_true_api_session_token

        result = get_true_api_session_token()

        self.assertIsNone(result['token'])
        self.assertIn('не отвечает', result['message'])

    @patch('app_cz.services.suz_client.attached_signed_data',
           side_effect=RuntimeError('Сервис подписей недоступен'))
    @patch('app_cz.services.suz_client.get_true_api_auth_key',
           return_value={'uuid': 'u-1', 'data': 'data'})
    def test_sign_error_returns_no_token_not_raise(self, *_):
        from app_cz.services.suz_client import get_true_api_session_token

        result = get_true_api_session_token()

        self.assertIsNone(result['token'])
        self.assertIn('подпис', result['message'].lower())

    @patch('app_cz.services.suz_client.attached_signed_data',
           return_value=('data', 'SIGNED'))
    @patch('app_cz.services.suz_client.get_true_api_auth_key',
           return_value={'uuid': 'u-1', 'data': 'data'})
    @patch('app_cz.services.suz_client.requests.post')
    def test_http_error_returns_message(self, mock_post, *_):
        from app_cz.services.suz_client import get_true_api_session_token

        response = Mock()
        response.status_code = 401
        response.json.return_value = {'error_message': 'Неверная подпись'}
        response.text = '{"error_message": "Неверная подпись"}'
        mock_post.return_value = response

        result = get_true_api_session_token()

        self.assertIsNone(result['token'])
        self.assertIn('Неверная подпись', result['message'])


class ReservePartiesTokenFailureTests(TestCase):
    """Резерв партий не падает 500 при сбое получения токена TrueAPI."""

    def test_reserve_returns_error_dict_on_token_failure(self):
        with patch.object(
            party_service,
            'get_true_api_session_token',
            return_value={'uuid': None, 'token': None,
                          'message': 'Сервис ЧЗ не отвечает'},
        ):
            result = party_service.reserve_parties_honest_sign(
                product_group='milk',
                party_numbers=['046017510249302610010000-2640501'],
            )

        self.assertTrue(result['is_error'])
        self.assertIn('Сервис ЧЗ не отвечает', result['message_error'])



class CodeSyncFilterTests(TestCase):
    """Синхронизация кодов: только «В работе»/«Закрыто» и за последние 3 дня."""

    def setUp(self):
        from app_factory.models import Line, Workshop

        self.factory = Factory.objects.create(
            name='Завод фильтра', ip_address='127.0.0.1', port_address=8030,
        )
        workshop = Workshop.objects.create(factory=self.factory, name='Цех')
        self.line = Line.objects.create(workshop=workshop, name='Линия')

    def _party(self, number, status, created):
        return ProductionParty.objects.create(
            line=self.line,
            production_party=number,
            external_number_task=number,
            is_external=True,
            status=status,
            external_created_at=created,
        )

    def _synced_numbers(self):
        synced = []

        def _fake(party):
            synced.append(party.external_number_task)
            return {'has_error': False, 'synced_count': 0, 'updated_count': 0}

        with patch.object(code_sync, 'sync_codes_for_party', side_effect=_fake):
            code_sync.sync_all_external_tasks()
        return synced

    def test_only_work_and_closed_recent(self):
        now = timezone.now()
        self._party('work', ProductionPartyStatusChoices.WORK, now)
        self._party('closed', ProductionPartyStatusChoices.CLOSED, now)
        self._party('created', ProductionPartyStatusChoices.CREATED, now)
        self._party('completed', ProductionPartyStatusChoices.COMPLETED, now)

        synced = self._synced_numbers()

        self.assertIn('work', synced)
        self.assertIn('closed', synced)
        self.assertNotIn('created', synced)
        self.assertNotIn('completed', synced)

    def test_old_tasks_excluded(self):
        from datetime import timedelta as _td
        now = timezone.now()
        self._party('today', ProductionPartyStatusChoices.WORK, now)
        self._party('d3', ProductionPartyStatusChoices.WORK, now - _td(days=3))
        self._party('d4', ProductionPartyStatusChoices.WORK, now - _td(days=4))

        synced = self._synced_numbers()

        self.assertIn('today', synced)
        self.assertIn('d3', synced)
        self.assertNotIn('d4', synced)

    def test_receive_maps_datetime_create(self):
        code_sync.receive_external_task({
            'uuid_str': 'task-dt-1',
            'status': 'В работе',
            'datetime_create': '2026-10-01T12:30:00.000Z',
        })

        party = ProductionParty.objects.get(external_number_task='task-dt-1')
        self.assertIsNotNone(party.external_created_at)
        self.assertEqual(party.external_created_at.year, 2026)
        self.assertEqual(party.external_created_at.month, 10)
        self.assertEqual(party.external_created_at.day, 1)



class GenerateCzUipFormatTests(TestCase):
    """Генерация через ЧЗ отправляет productionDate полным ISO 8601 (…Z)."""

    def setUp(self):
        self.sku = _create_sku()

    def test_generate_party_numbers_sends_iso_datetime(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'inn': '7701234567',
            'partyNumberInfo': [{
                'partyNumber': '04601751026019261002000000000000',
                'gtin': '04601751026019',
                'productionDate': '2026-10-02T00:00:00.000Z',
            }],
        }
        response.text = ''

        with patch.object(
            party_service, 'get_true_api_session_token',
            return_value={'uuid': 'u', 'token': 'T', 'message': 'ok'},
        ), patch(
            'app_cz.services.party_service.requests.post', return_value=response,
        ) as mock_post:
            result = party_service.generate_party_numbers(
                party_info_list=[{
                    'gtin': '04601751026019',
                    'productionDate': '2026-10-02T00:00:00.000Z',
                    'count': 1,
                }],
                product_group='milk',
            )

        self.assertFalse(result['is_error'])
        sent = mock_post.call_args.kwargs['data']
        self.assertIn('T00:00:00.000Z', sent)

    def test_cz_uip_uses_iso_production_date(self):
        from datetime import date as _date

        captured = {}

        def _fake_generate(party_info_list, product_group):
            captured['info'] = party_info_list
            return {
                'is_error': False,
                'lst_party_number_info': [{
                    'partyNumber': '04601751026019261002000000000000',
                }],
            }

        with patch.object(
            party_service, 'generate_party_numbers', side_effect=_fake_generate,
        ):
            result = party_service._generate_cz_uip(
                self.sku, self.sku.product.consumer_gtin, _date(2026, 10, 2),
            )

        self.assertFalse(result.get('is_error'))
        info = captured['info'][0]
        self.assertEqual(info['productionDate'], '2026-10-02T00:00:00.000Z')



class CheckUipCzEndpointTests(TestCase):
    """POST /cz/api/check-uip-cz/ — проверка УИП в рассинхроне."""

    def setUp(self):
        from app_cz import views as views_module
        self.views_module = views_module
        self.sku = _create_sku()
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
            is_desync=True,
        )
        self.url = '/cz/api/check-uip-cz/'

    def _login(self):
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

    def test_successful_reserve_restores_local_status(self):
        self._login()
        with patch.object(
            self.views_module, 'reserve_party_numbers_cz',
            return_value={
                'is_error': False, 'message_error': 'ОК',
                'lst_party_number_info': [{'partyNumber': self.uip.number}],
            },
        ):
            response = self.client.post(
                self.url, data={'uip_id': str(self.uip.id)},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['is_error'])
        self.assertEqual(body['result'], 'reserved')
        self.uip.refresh_from_db()
        self.assertEqual(self.uip.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertFalse(self.uip.is_desync)

    def test_failed_reserve_reports_registered(self):
        self._login()
        with patch.object(
            self.views_module, 'reserve_party_numbers_cz',
            return_value={'is_error': True, 'message_error': 'Номер уже занят'},
        ):
            response = self.client.post(
                self.url, data={'uip_id': str(self.uip.id)},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['result'], 'registered')
        self.uip.refresh_from_db()
        # Статус не меняется при отказе ЧЗ.
        self.assertEqual(self.uip.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertTrue(self.uip.is_desync)

    def test_unknown_uip_returns_404(self):
        self._login()
        response = self.client.post(
            self.url,
            data={'uip_id': '00000000-0000-0000-0000-000000000000'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)



class RefreshSuzTokenRetryTests(TestCase):
    """Обновление динамического токена СУЗ: повторы и проактивность."""

    def setUp(self):
        from app_cz.models import SUZAccount
        self.account = SUZAccount.objects.create(
            is_active=True,
            certificate_name='Сертификат',
            serial_number='0123456789ABCDEF',
            inn='7701234567',
            oms_id='oms-1',
            device_name='Устройство',
            connection_identifier='conn-1',
        )

    def test_refresh_retries_three_times_then_stops(self):
        from app_cz.services import suz_client

        with patch.object(
            suz_client, '_refresh_suz_dynamic_token_once', return_value=False,
        ) as mock_once:
            result = suz_client.refresh_suz_dynamic_token()

        self.assertFalse(result)
        self.assertEqual(mock_once.call_count, 3)

    def test_refresh_stops_after_first_success(self):
        from app_cz.services import suz_client

        with patch.object(
            suz_client, '_refresh_suz_dynamic_token_once', return_value=True,
        ) as mock_once:
            result = suz_client.refresh_suz_dynamic_token()

        self.assertTrue(result)
        self.assertEqual(mock_once.call_count, 1)

    def test_ensure_refreshes_within_one_hour(self):
        from app_cz.services import suz_client

        self.account.dynamic_token = 'TOKEN'
        self.account.token_expires_at = timezone.now() + timedelta(minutes=30)
        self.account.save(update_fields=['dynamic_token', 'token_expires_at'])

        with patch.object(
            suz_client, 'refresh_suz_dynamic_token', return_value=True,
        ) as mock_refresh:
            result = suz_client.ensure_suz_token_valid()

        self.assertTrue(mock_refresh.called)
        self.assertTrue(result['refreshed'])

    def test_ensure_skips_when_token_fresh(self):
        from app_cz.services import suz_client

        self.account.dynamic_token = 'TOKEN'
        self.account.token_expires_at = timezone.now() + timedelta(hours=5)
        self.account.save(update_fields=['dynamic_token', 'token_expires_at'])

        with patch.object(
            suz_client, 'refresh_suz_dynamic_token', return_value=True,
        ) as mock_refresh:
            result = suz_client.ensure_suz_token_valid()

        self.assertFalse(mock_refresh.called)
        self.assertTrue(result['skipped'])

    def test_ensure_refreshes_when_no_token(self):
        from app_cz.services import suz_client

        with patch.object(
            suz_client, 'refresh_suz_dynamic_token', return_value=True,
        ) as mock_refresh:
            result = suz_client.ensure_suz_token_valid()

        self.assertTrue(mock_refresh.called)
        self.assertTrue(result['refreshed'])
