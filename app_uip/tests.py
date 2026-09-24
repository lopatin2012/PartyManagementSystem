from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app_factory.models import (
    Product,
    ProductPackaging,
    ProductSKU,
    PackagingLevelChoices,
    ProductGroupChoices,
    StateConditionChoices,
    CardStateChoices,
    TypeFormationUIP,
    Factory,
    Workshop,
    Line,
)

from app_cz.models import CISCode
from app_cz.services.party_service import build_local_party_number

from app_helper.access import (
    ROLE_ADMIN,
    ROLE_VIEW,
    is_admin,
    can_view_uip,
    can_generate_uip,
)
from app_helper.search_helper import filter_codes_by_query, filter_uips_by_query

from app_uip.models import UIP, ProductionParty, PartyStatusChoices
from app_uip.serializers import (
    UIPReserveItemSerializer,
    UIPReserveRequestSerializer,
)
from app_uip.services.uip_reserve import reserve_uips
from app_uip.services.reserve_accumulation import (
    accumulate_short_shelf_life_reserve,
)


# ==========================================
# Fixtures.
# ==========================================

def create_product(article: str = '50032', gtin: str = '04601751026019') -> ProductSKU:
    """Создаёт продукт с потребительской упаковкой и SKU."""
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
        gtin=gtin,
        quantity_inside=1,
    )
    return ProductSKU.objects.create(
        product=product,
        article=article,
    )


# ==========================================
# Тесты сериализаторов.
# ==========================================

class UIPReserveItemSerializerTests(TestCase):
    """Проверка валидации одного запроса резервирования."""

    def test_generate_valid(self):
        serializer = UIPReserveItemSerializer(data={
            'article': '50032',
            'production_date': '2026-01-01',
            'mode': 'local',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_generate_by_gtin_valid(self):
        serializer = UIPReserveItemSerializer(data={
            'gtin': '04601751026019',
            'production_date': '2026-01-01',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_reserve_own_valid(self):
        serializer = UIPReserveItemSerializer(data={
            'product_group': 'milk',
            'party_numbers': ['04601751026019260101500320000000'],
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_missing_both_paths_invalid(self):
        serializer = UIPReserveItemSerializer(data={
            'production_date': '2026-01-01',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            'Укажите article/gtin',
            ' '.join(str(e) for e in serializer.errors.get('non_field_errors', [])),
        )

    def test_both_paths_invalid(self):
        serializer = UIPReserveItemSerializer(data={
            'article': '50032',
            'production_date': '2026-01-01',
            'product_group': 'milk',
            'party_numbers': ['04601751026019260101500320000000'],
        })
        self.assertFalse(serializer.is_valid())

    def test_generate_requires_production_date(self):
        serializer = UIPReserveItemSerializer(data={
            'article': '50032',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            'production_date',
            ' '.join(str(e) for e in serializer.errors.get('non_field_errors', [])),
        )

    def test_reserve_own_requires_product_group(self):
        serializer = UIPReserveItemSerializer(data={
            'party_numbers': ['04601751026019260101500320000000'],
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            'product_group',
            ' '.join(str(e) for e in serializer.errors.get('non_field_errors', [])),
        )


class UIPReserveRequestSerializerTests(TestCase):
    """Проверка приёма одного объекта или списка объектов."""

    def test_accepts_single_dict(self):
        serializer = UIPReserveRequestSerializer(data={
            'article': '50032',
            'production_date': '2026-01-01',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsInstance(serializer.validated_data, dict)

    def test_accepts_list(self):
        serializer = UIPReserveRequestSerializer(data=[
            {'article': '50032', 'production_date': '2026-01-01'},
            {'product_group': 'milk', 'party_numbers': ['04601751026019260101500320000000']},
        ])
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsInstance(serializer.validated_data, list)
        self.assertEqual(len(serializer.validated_data), 2)

    def test_rejects_invalid_element(self):
        serializer = UIPReserveRequestSerializer(data=[
            {'article': '50032'},
        ])
        self.assertFalse(serializer.is_valid())

    def test_rejects_other_type(self):
        serializer = UIPReserveRequestSerializer(data='article')
        self.assertFalse(serializer.is_valid())


# ==========================================
# Тесты сервиса reserve_uips.
# ==========================================

class ReserveUipsServiceTests(TestCase):
    """Проверка общего метода резервирования."""

    def setUp(self):
        self.sku = create_product()

    def test_single_generate_routes_through_generate_uip(self):
        with patch('app_uip.services.uip_reserve.generate_uip') as mock_generate:
            mock_generate.return_value = {
                'is_error': False,
                'number': '04601751026019260101500320000000',
                'uuid_uip': '00000000-0000-0000-0000-000000000001',
                'message': 'УИП создан',
            }
            result = reserve_uips({
                'article': '50032',
                'production_date': '2026-01-01',
                'mode': 'local',
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(result['count'], 1)
        mock_generate.assert_called_once()
        self.assertIn('number', result['results'][0])

    def test_generate_without_sku_returns_error(self):
        result = reserve_uips({
            'article': 'НЕСУЩЕСТВУЮЩИЙ',
            'production_date': '2026-01-01',
        })
        self.assertTrue(result['is_error'])
        self.assertEqual(result['results'][0]['message'], 'Продукт не найден или неактивен (укажите article или gtin).')

    def test_generate_count_loops(self):
        with patch('app_uip.services.uip_reserve.generate_uip') as mock_generate:
            mock_generate.return_value = {
                'is_error': False,
                'number': '04601751026019260101500320000000',
                'uuid_uip': '00000000-0000-0000-0000-000000000001',
            }
            result = reserve_uips({
                'article': '50032',
                'production_date': '2026-01-01',
                'count': 3,
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(mock_generate.call_count, 3)
        self.assertEqual(result['count'], 3)

    def test_generate_breaks_on_first_error(self):
        with patch('app_uip.services.uip_reserve.generate_uip') as mock_generate:
            mock_generate.return_value = {
                'is_error': True,
                'message': 'ЧЗ отклонил резервирование',
            }
            result = reserve_uips({
                'article': '50032',
                'production_date': '2026-01-01',
                'count': 3,
            })

        self.assertTrue(result['is_error'])
        mock_generate.assert_called_once()
        self.assertIn('ЧЗ отклонил резервирование', result['results'][0]['message'])

    def test_reserve_own_routes_through_reserve_parties_honest_sign(self):
        with patch('app_uip.services.uip_reserve.reserve_parties_honest_sign') as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'Ошибки отсутствуют',
                'lst_party_number_info': [
                    {'partyNumber': '04601751026019260101500320000000'},
                ],
            }
            result = reserve_uips({
                'product_group': 'milk',
                'party_numbers': ['04601751026019260101500320000000'],
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(result['count'], 1)
        mock_reserve.assert_called_once_with(
            product_group='milk',
            party_numbers=['04601751026019260101500320000000'],
        )

    def test_reserve_own_error_propagates(self):
        with patch('app_uip.services.uip_reserve.reserve_parties_honest_sign') as mock_reserve:
            mock_reserve.return_value = {
                'is_error': True,
                'message_error': 'ЧЗ отклонил резервирование',
            }
            result = reserve_uips({
                'product_group': 'milk',
                'party_numbers': ['04601751026019260101500320000000'],
            })

        self.assertTrue(result['is_error'])
        self.assertIn('ЧЗ отклонил резервирование', result['results'][0]['message'])

    def test_reserve_own_missing_product_group(self):
        result = reserve_uips({
            'party_numbers': ['04601751026019260101500320000000'],
        })
        self.assertTrue(result['is_error'])
        self.assertIn('product_group', result['results'][0]['message'])

    def test_dict_input_normalized_to_list(self):
        with patch('app_uip.services.uip_reserve.generate_uip') as mock_generate:
            mock_generate.return_value = {'is_error': False, 'number': 'x', 'uuid_uip': 'y'}
            result = reserve_uips({
                'article': '50032',
                'production_date': '2026-01-01',
            })
        self.assertFalse(result['is_error'])
        self.assertEqual(len(result['results']), 1)

    def test_list_of_mixed_requests(self):
        with patch('app_uip.services.uip_reserve.generate_uip') as mock_generate, \
             patch('app_uip.services.uip_reserve.reserve_parties_honest_sign') as mock_reserve:
            mock_generate.return_value = {'is_error': False, 'number': 'x', 'uuid_uip': 'y'}
            mock_reserve.return_value = {
                'is_error': False,
                'lst_party_number_info': [{'partyNumber': 'z'}],
            }
            result = reserve_uips([
                {'article': '50032', 'production_date': '2026-01-01'},
                {'product_group': 'milk', 'party_numbers': ['z']},
            ])

        self.assertFalse(result['is_error'])
        self.assertEqual(len(result['results']), 2)
        mock_generate.assert_called_once()
        mock_reserve.assert_called_once()

    def test_empty_list(self):
        result = reserve_uips([])
        self.assertTrue(result['is_error'])
        self.assertEqual(result['message'], 'Пустой список запросов на резервирование.')

    def test_non_dict_non_list_input(self):
        result = reserve_uips('article')
        self.assertTrue(result['is_error'])
        self.assertEqual(result['message'], 'Запрос должен быть объектом или списком объектов.')

    def test_skip_cz_generate_creates_draft_uip(self):
        """Полная проверка пути: реальный generate_uip с skip_cz создаёт черновик."""
        result = reserve_uips({
            'article': '50032',
            'production_date': '2026-01-01',
            'mode': 'local',
            'skip_cz': True,
        })

        self.assertFalse(result['is_error'])
        number = result['results'][0]['number']
        uip = UIP.objects.filter(number=number).first()
        self.assertIsNotNone(uip)
        self.assertEqual(uip.status, PartyStatusChoices.DRAFT)


# ==========================================
# Тесты ручного ввода УИП.
# ==========================================

class ReserveManualUipTests(TestCase):
    """Проверка reserve_manual_uip (ручной ввод серийной части)."""

    def setUp(self):
        self.sku = create_product()

    def test_builds_and_reserves_number(self):
        from app_cz.services.party_service import reserve_manual_uip

        with patch(
            'app_cz.services.party_service.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'ОК',
                'lst_party_number_info': [],
            }
            result = reserve_manual_uip(
                self.sku, date(2026, 1, 15), 'ABC123456789',
            )

        self.assertFalse(result['is_error'])
        number = '04601751026019' + '260115' + 'ABC123456789'
        self.assertEqual(result['number'], number)
        self.assertEqual(len(number), 32)
        uip = UIP.objects.get(number=number)
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertEqual(uip.production_date, date(2026, 1, 15))
        mock_reserve.assert_called_once()

    def test_invalid_serial_returns_error(self):
        from app_cz.services.party_service import reserve_manual_uip

        result = reserve_manual_uip(self.sku, date(2026, 1, 15), 'AB!@#')
        self.assertTrue(result['is_error'])
        self.assertEqual(UIP.objects.count(), 0)

    def test_wrong_length_returns_error(self):
        from app_cz.services.party_service import reserve_manual_uip

        # GTIN(14)+дата(6)=20, серийная 1 → 21, не 32.
        result = reserve_manual_uip(self.sku, date(2026, 1, 15), 'A')
        self.assertTrue(result['is_error'])
        self.assertIn('32', result['message'])

    def test_existing_number_returns_error(self):
        from app_cz.services.party_service import reserve_manual_uip

        number = '04601751026019' + '260115' + 'ABC123456789'
        UIP.objects.create(
            product_sku=self.sku, number=number,
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        result = reserve_manual_uip(self.sku, date(2026, 1, 15), 'ABC123456789')
        self.assertTrue(result['is_error'])
        self.assertIn('уже существует', result['message'])


# ==========================================
# Тесты эндпоинта.
# ==========================================

class ReserveUipsEndpointTests(TestCase):
    """Проверка POST /uip/api/v1/reserve-uips/."""

    def setUp(self):
        self.sku = create_product()
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@test.local', password='pass'
        )
        self.user = User.objects.create_user(
            username='user', email='user@test.local', password='pass'
        )
        self.url = '/uip/api/v1/reserve-uips/'

    def test_requires_admin(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {'article': '50032', 'production_date': '2026-01-01'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_single_generate_returns_200(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {'article': '50032', 'production_date': '2026-01-01', 'mode': 'local', 'skip_cz': True},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['is_error'])
        self.assertEqual(data['count'], 1)

    def test_list_returns_200(self):
        self.client.force_login(self.admin)
        with patch('app_uip.services.uip_reserve.reserve_parties_honest_sign') as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'Ошибки отсутствуют',
                'lst_party_number_info': [
                    {'partyNumber': '04601751026019260101500320000000'},
                ],
            }
            response = self.client.post(
                self.url,
                [
                    {'article': '50032', 'production_date': '2026-01-01', 'mode': 'local', 'skip_cz': True},
                    {'product_group': 'milk', 'party_numbers': ['04601751026019260101500320000000']},
                ],
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['results']), 2)

    def test_invalid_payload_returns_400(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {'article': '50032'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()['is_error'])


class ReserveUipsSchemaTests(TestCase):
    """
    Проверка, что параметры эндпоинта резервирования видны в OpenAPI-схеме
    (/api/docs). Регрессионный тест: UIPReserveRequestSerializer должен
    объявлять поля, иначе drf-spectacular не сгенерирует requestBody.
    """

    PATH = '/uip/api/v1/reserve-uips/'
    EXPECTED_FIELDS = [
        'article',
        'gtin',
        'production_date',
        'mode',
        'count',
        'party',
        'target_status',
        'skip_cz',
        'product_group',
        'party_numbers',
    ]

    def test_schema_includes_request_fields(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema.get('paths', {})

        self.assertIn(self.PATH, paths)
        post_operation = paths[self.PATH].get('post', {})
        self.assertIn('requestBody', post_operation)

        content = post_operation['requestBody']['content']
        self.assertIn('application/json', content)
        schema_ref = content['application/json']['schema']

        # Разыменовываем $ref до компонента схемы запроса.
        if '$ref' in schema_ref:
            ref_name = schema_ref['$ref'].rsplit('/', 1)[-1]
            component = schema['components']['schemas'].get(ref_name, {})
        else:
            component = schema_ref

        properties = component.get('properties', {})
        for field in self.EXPECTED_FIELDS:
            self.assertIn(field, properties, f'Поле "{field}" отсутствует в схеме запроса.')


# ==========================================
# Повторное резервирование сгоревших УИП.
# ==========================================

BURNED_GTIN = '04601751026019'
BURNED_ARTICLE = '50032'
BURNED_DATE = date(2026, 1, 1)


def burned_uip_number() -> str:
    """Детерминированный номер УИП тестового продукта (режим local)."""
    return build_local_party_number(
        BURNED_GTIN,
        BURNED_DATE,
        BURNED_ARTICLE,
        '000',
        TypeFormationUIP.general.value,
    )


class ReReserveBurnedUipTests(TestCase):
    """
    УИП, «сгоревший» за 30 дней неиспользования (status=deleted), должен
    повторно резервироваться тем же номером, если по нему пришёл запрос
    на использование (генерация или резервирование своих номеров).
    """

    def setUp(self):
        self.sku = create_product(article=BURNED_ARTICLE, gtin=BURNED_GTIN)

    def _create_burned_uip(self, reservation_date=date(2025, 1, 1)):
        return UIP.objects.create(
            product_sku=self.sku,
            number=burned_uip_number(),
            status=PartyStatusChoices.DELETED,
            reservation_date=reservation_date,
        )

    def test_generate_re_reserves_burned_uip(self):
        uip = self._create_burned_uip()
        uip.is_desync = True
        uip.save(update_fields=['is_desync'])

        with patch(
            'app_cz.services.party_service.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'Ошибки отсутствуют',
                'lst_party_number_info': [{'partyNumber': uip.number}],
            }
            result = reserve_uips({
                'article': BURNED_ARTICLE,
                'production_date': BURNED_DATE.isoformat(),
                'mode': 'local',
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(result['count'], 1)
        mock_reserve.assert_called_once()

        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertEqual(uip.reservation_date, timezone.now().date())
        self.assertFalse(uip.is_desync)

    def test_generate_cz_rejection_keeps_uip_burned(self):
        uip = self._create_burned_uip()

        with patch(
            'app_cz.services.party_service.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': True,
                'message_error': 'УИП уже зарезервирован',
            }
            result = reserve_uips({
                'article': BURNED_ARTICLE,
                'production_date': BURNED_DATE.isoformat(),
                'mode': 'local',
            })

        self.assertTrue(result['is_error'])
        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.DELETED)
        self.assertEqual(uip.reservation_date, date(2025, 1, 1))

    def test_generate_existing_active_uip_not_re_reserved(self):
        uip = self._create_burned_uip()
        uip.change_status(PartyStatusChoices.RESERVED_LOCAL, source='test')

        with patch(
            'app_cz.services.party_service.reserve_parties_honest_sign'
        ) as mock_reserve:
            result = reserve_uips({
                'article': BURNED_ARTICLE,
                'production_date': BURNED_DATE.isoformat(),
                'mode': 'local',
            })

        self.assertTrue(result['is_error'])
        mock_reserve.assert_not_called()
        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_LOCAL)

    def test_reserve_own_restores_burned_uip(self):
        uip = self._create_burned_uip()

        with patch(
            'app_uip.services.uip_reserve.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'Ошибки отсутствуют',
                'lst_party_number_info': [{'partyNumber': uip.number}],
            }
            result = reserve_uips({
                'product_group': 'milk',
                'party_numbers': [uip.number],
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(result['results'][0]['restored'], [uip.number])

        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertEqual(uip.reservation_date, timezone.now().date())

    def test_reserve_own_does_not_touch_active_uip(self):
        uip = self._create_burned_uip()
        uip.change_status(PartyStatusChoices.RESERVED_CZ, source='test')

        with patch(
            'app_uip.services.uip_reserve.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = {
                'is_error': False,
                'message_error': 'Ошибки отсутствуют',
                'lst_party_number_info': [{'partyNumber': uip.number}],
            }
            result = reserve_uips({
                'product_group': 'milk',
                'party_numbers': [uip.number],
            })

        self.assertFalse(result['is_error'])
        self.assertEqual(result['results'][0]['restored'], [])
        uip.refresh_from_db()
        self.assertEqual(uip.status, PartyStatusChoices.RESERVED_CZ)
        self.assertEqual(uip.reservation_date, date(2025, 1, 1))


# ==========================================
# Единый поиск (УИП / код маркировки).
# ==========================================

class UIPSearchEndpointTests(TestCase):
    """Проверка GET /uip/api/v1/search/."""

    URL = '/uip/api/v1/search/'
    GTIN = '04601751026019'
    UIP_NUMBER = '04601751026019260101500320000000'
    CODE = '010460175102601921ABC123'

    def setUp(self):
        self.sku = create_product(gtin=self.GTIN)
        self.packaging = ProductPackaging.objects.get(gtin=self.GTIN)
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number=self.UIP_NUMBER,
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.party = ProductionParty.objects.create(
            uip=self.uip,
            production_party='1',
        )
        self.code = CISCode.objects.create(
            production_party=self.party,
            product_packaging=self.packaging,
            code=self.CODE,
            level=PackagingLevelChoices.UNIT,
        )

    def test_search_by_uip_number(self):
        response = self.client.get(self.URL, {'q': self.UIP_NUMBER})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['search_type'], 'uip')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['number'], self.UIP_NUMBER)

    def test_search_by_code(self):
        response = self.client.get(self.URL, {'q': self.CODE})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['search_type'], 'code')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['code'], self.CODE)
        self.assertEqual(data['results'][0]['uip_number'], self.UIP_NUMBER)
        self.assertEqual(data['results'][0]['gtin'], self.GTIN)

    def test_search_by_code_with_gs_separator(self):
        response = self.client.get(
            self.URL, {'q': '0104601751026019\x1D21ABC123'}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['search_type'], 'code')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['results'][0]['code'], self.CODE)

    def test_type_override_forces_uip(self):
        response = self.client.get(self.URL, {'q': self.CODE, 'type': 'uip'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['search_type'], 'uip')
        self.assertEqual(data['count'], 0)

    def test_missing_query_returns_400(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()['is_error'])

    def test_not_found_returns_empty(self):
        response = self.client.get(
            self.URL, {'q': '04601751026019260101500320999999'}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['results'], [])

    def test_pagination(self):
        for suffix in ('4', '5'):
            CISCode.objects.create(
                production_party=self.party,
                product_packaging=self.packaging,
                code=f'010460175102601921ABC12{suffix}',
                level=PackagingLevelChoices.UNIT,
            )
        response = self.client.get(
            self.URL,
            {'q': '010460175102601921', 'page_size': 1, 'page': 2},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['search_type'], 'code')
        self.assertEqual(data['count'], 3)
        self.assertEqual(data['page'], 2)
        self.assertEqual(data['page_size'], 1)
        self.assertEqual(len(data['results']), 1)


class UIPActiveListEndpointTests(TestCase):
    """
    Регрессия: product_name должен браться из product_sku.product.name
    (у ProductSKU нет собственного поля name).
    """

    def test_active_list_includes_product_name(self):
        sku = create_product()
        UIP.objects.create(
            product_sku=sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        response = self.client.get('/uip/api/v1/status_parties/active/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['result'][0]['product_name'], 'Тестовый продукт')





# ==========================================
# Роли и доступ (группы Django).
# ==========================================

# В тестах DEBUG выключен, а ManifestStaticFilesStorage требует собранный
# манифест. Для рендер-тестов подменяем статику на простую.
STATIC_OVERRIDE = override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})


@STATIC_OVERRIDE
class UIPListReservedCountTests(TestCase):
    """Счётчик зарезервированных УИП на странице /uip/."""

    def test_reserved_count_excludes_drafts(self):
        sku = create_product()
        UIP.objects.create(
            product_sku=sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        UIP.objects.create(
            product_sku=sku,
            number='04601751026019260101500320000001',
            status=PartyStatusChoices.RESERVED_CZ,
        )
        UIP.objects.create(
            product_sku=sku,
            number='04601751026019260101500320000002',
            status=PartyStatusChoices.DRAFT,
        )

        user = User.objects.create_superuser(
            username='admin', password='pass', email='a@a.a'
        )
        self.client.force_login(user)
        response = self.client.get('/uip/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 3)
        self.assertEqual(response.context['reserved_count'], 2)


class AccessRolesTests(TestCase):
    """Проверка ролевых хелперов (Админ / Просмотр)."""

    def setUp(self):
        self.admin_group, _ = Group.objects.get_or_create(name=ROLE_ADMIN)
        self.view_group, _ = Group.objects.get_or_create(name=ROLE_VIEW)

    def _user(self, username, groups=(), **extra):
        user = User.objects.create_user(username=username, password='pass', **extra)
        for group in groups:
            user.groups.add(group)
        return user

    def test_superuser_is_admin(self):
        root = User.objects.create_superuser('root', 'root@example.com', 'pass')
        self.assertTrue(is_admin(root))

    def test_admin_group_member_is_admin(self):
        user = self._user('adm', [self.admin_group])
        self.assertTrue(is_admin(user))
        self.assertTrue(can_generate_uip(user))

    def test_view_group_member_can_view_not_generate(self):
        user = self._user('viewer', [self.view_group])
        self.assertFalse(is_admin(user))
        self.assertTrue(can_view_uip(user))
        self.assertFalse(can_generate_uip(user))

    def test_viewer_with_add_uip_can_generate(self):
        user = self._user('viewer-writer', [self.view_group])
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='app_uip', codename='add_uip'
            )
        )
        self.assertTrue(can_generate_uip(user))

    def test_user_without_roles_has_no_uip_access(self):
        user = self._user('nobody')
        self.assertFalse(can_view_uip(user))
        self.assertFalse(can_generate_uip(user))


@STATIC_OVERRIDE
class UIPPageAccessTests(TestCase):
    """Доступ к странице УИП и генерации по ролям."""

    def setUp(self):
        self.view_group, _ = Group.objects.get_or_create(name=ROLE_VIEW)

    def _viewer(self, username, with_write=False):
        user = User.objects.create_user(username=username, password='pass')
        user.groups.add(self.view_group)
        if with_write:
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label='app_uip', codename='add_uip'
                )
            )
        return user

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse('uip_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login/', response.url)

    def test_viewer_can_open_uip_page(self):
        self.client.force_login(self._viewer('viewer'))
        self.assertEqual(self.client.get(reverse('uip_list')).status_code, 200)

    def test_superuser_can_open_uip_page(self):
        root = User.objects.create_superuser('root-uip', 'root-uip@example.com', 'pass')
        self.client.force_login(root)
        self.assertEqual(self.client.get(reverse('uip_list')).status_code, 200)

    def test_user_without_role_forbidden(self):
        user = User.objects.create_user(username='nobody', password='pass')
        self.client.force_login(user)
        response = self.client.get(reverse('uip_list'))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'Доступ запрещён', status_code=403)

    def test_viewer_without_write_cannot_generate(self):
        self.client.force_login(self._viewer('viewer-nowrite'))
        response = self.client.post(
            reverse('uip_generate'), data='{}', content_type='application/json'
        )
        self.assertEqual(response.status_code, 403)

    def test_viewer_with_write_can_reach_generate(self):
        self.client.force_login(self._viewer('viewer-write', with_write=True))
        response = self.client.post(
            reverse('uip_generate'), data='{}', content_type='application/json'
        )
        # Доступ есть: пустое тело даёт 400 (нет параметров), но не 403.
        self.assertEqual(response.status_code, 400)

    def test_admin_page_requires_admin(self):
        self.client.force_login(self._viewer('viewer-adminpage'))
        self.assertEqual(self.client.get(reverse('sync_tasks')).status_code, 403)


# ==========================================
# Результаты поиска — карточки и внешнее задание.
# ==========================================

@STATIC_OVERRIDE
class SearchResultsCardTests(TestCase):
    """Поиск отдаёт карточку с внешним номером задания и местом производства."""

    def setUp(self):
        self.sku = create_product()
        self.factory = Factory.objects.create(name='Завод Тестовый')
        self.workshop = Workshop.objects.create(factory=self.factory, name='Цех Тестовый')
        self.line = Line.objects.create(workshop=self.workshop, name='Линия Тестовая')
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.party = ProductionParty.objects.create(
            uip=self.uip,
            line=self.line,
            external_number_task='TASK-EXTERNAL-1',
            production_party='145',
        )
        packaging = self.sku.product.packagings.first()
        self.code = CISCode.objects.create(
            production_party=self.party,
            product_packaging=packaging,
            code='01046017510260192150abc',
            level=PackagingLevelChoices.UNIT,
        )

    def test_code_search_shows_external_task_and_place(self):
        response = self.client.get(reverse('search'), {'q': self.code.code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['search_type'], 'code')
        self.assertContains(response, 'TASK-EXTERNAL-1')
        self.assertContains(response, 'Завод Тестовый')
        self.assertContains(response, 'Цех Тестовый')
        self.assertContains(response, 'Линия Тестовая')

    def test_uip_search_shows_external_task(self):
        response = self.client.get(reverse('search'), {'q': self.uip.number})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['search_type'], 'uip')
        self.assertContains(response, 'TASK-EXTERNAL-1')
        self.assertContains(response, 'Завод Тестовый')


class SearchQueryHelpersTests(TestCase):
    """Точное совпадение вперёд, префикс и регистронезависимый запасной вариант."""

    def setUp(self):
        self.sku = create_product()
        self.uip = UIP.objects.create(
            product_sku=self.sku,
            number='04601751026019260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.party = ProductionParty.objects.create(
            uip=self.uip,
            external_number_task='TASK-HELPER-1',
            production_party='145',
        )
        packaging = self.sku.product.packagings.first()
        self.code = CISCode.objects.create(
            production_party=self.party,
            product_packaging=packaging,
            code='01046017510260192150abc',
            level=PackagingLevelChoices.UNIT,
        )

    def test_codes_exact_match(self):
        qs = filter_codes_by_query(CISCode.objects.all(), self.code.code)
        self.assertEqual(list(qs.values_list('code', flat=True)), [self.code.code])

    def test_codes_prefix_fallback(self):
        qs = filter_codes_by_query(CISCode.objects.all(), self.code.code[:12])
        self.assertIn(self.code.code, list(qs.values_list('code', flat=True)))

    def test_codes_case_insensitive_fallback(self):
        # Точного совпадения нет (другой регистр) — срабатывает istartswith.
        qs = filter_codes_by_query(CISCode.objects.all(), self.code.code.upper())
        self.assertIn(self.code.code, list(qs.values_list('code', flat=True)))

    def test_codes_with_group_separator_match_raw_query(self):
        # В БД коды могут содержать GS (\x1d) перед AI 93. Сканер отдаёт код
        # «сырым» (с GS) — поиск обязан его находить.
        packaging = self.sku.product.packagings.first()
        gs_code = '0104601751029423215\x1djeWO93XhW8'
        CISCode.objects.create(
            production_party=self.party,
            product_packaging=packaging,
            code=gs_code,
            level=PackagingLevelChoices.UNIT,
        )
        qs = filter_codes_by_query(CISCode.objects.all(), gs_code)
        self.assertEqual(list(qs.values_list('code', flat=True)), [gs_code])

    def test_uips_exact_match(self):
        qs = filter_uips_by_query(UIP.objects.all(), self.uip.number)
        self.assertEqual(list(qs.values_list('number', flat=True)), [self.uip.number])

    def test_uips_no_match(self):
        qs = filter_uips_by_query(UIP.objects.all(), '0' * 32)
        self.assertEqual(qs.count(), 0)


# ==========================================
# Тесты накопления резерва УИП на дни вперёд.
# ==========================================

class ReserveAccumulationTests(TestCase):
    """Проверка app_uip.services.reserve_accumulation."""

    def setUp(self):
        self.sku = create_product()
        self.sku.reserve_days = 2
        self.sku.save(update_fields=['reserve_days'])
        self.today = timezone.now().date()

    @staticmethod
    def _cz_success():
        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': [],
        }

    def _make_product(self, article, gtin, shelf_life, type_formation=None,
                      is_active=True):
        product = Product.objects.create(
            group=ProductGroupChoices.MILK,
            name=f'Продукт {article}',
            shelf_life_in_days=shelf_life,
            item_condition=StateConditionChoices.READY_ORDER_KM,
            card_status=CardStateChoices.PUBLISHED,
        )
        ProductPackaging.objects.create(
            product=product,
            level=PackagingLevelChoices.UNIT,
            gtin=gtin,
            quantity_inside=1,
        )
        return ProductSKU.objects.create(
            product=product,
            article=article,
            type_formation_uip=type_formation or TypeFormationUIP.general,
            is_active=is_active,
        )

    def _number_for(self, production_date):
        return build_local_party_number(
            '04601751026019',
            production_date,
            article='50032',
            party='000',
            type_formation_uip=TypeFormationUIP.general.value,
        )

    def test_default_mode_creates_drafts_without_cz(self):
        with patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve:
            result = accumulate_short_shelf_life_reserve(pause_seconds=0)

        self.assertFalse(result['is_error'])
        self.assertTrue(result['skip_cz'])
        self.assertEqual(result['created'], 3)  # сегодня, +1, +2
        mock_reserve.assert_not_called()  # в ЧЗ не обращаемся
        uips = UIP.objects.filter(product_sku=self.sku)
        self.assertEqual(uips.count(), 3)
        self.assertEqual(
            sorted(uips.values_list('production_date', flat=True)),
            [
                self.today,
                self.today + timedelta(days=1),
                self.today + timedelta(days=2),
            ],
        )
        self.assertEqual(
            set(uips.values_list('status', flat=True)),
            {PartyStatusChoices.DRAFT},
        )
        self.assertEqual(
            set(uips.values_list('reservation_date', flat=True)),
            {None},
        )

    def test_cz_mode_creates_reserved_uips(self):
        with patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = self._cz_success()
            result = accumulate_short_shelf_life_reserve(
                pause_seconds=0, skip_cz=False
            )

        self.assertFalse(result['is_error'])
        self.assertFalse(result['skip_cz'])
        self.assertEqual(result['created'], 3)
        mock_reserve.assert_called()
        uips = UIP.objects.filter(product_sku=self.sku)
        self.assertEqual(
            set(uips.values_list('status', flat=True)),
            {PartyStatusChoices.RESERVED_LOCAL},
        )

    def test_second_run_is_idempotent(self):
        with patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = self._cz_success()
            accumulate_short_shelf_life_reserve(pause_seconds=0, skip_cz=False)
            result = accumulate_short_shelf_life_reserve(
                pause_seconds=0, skip_cz=False
            )

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['restored'], 0)
        self.assertEqual(result['skipped_existing'], 3)
        self.assertEqual(UIP.objects.filter(product_sku=self.sku).count(), 3)

    def test_excludes_long_shelf_life_other_type_and_inactive(self):
        long_sku = self._make_product('LONG', '04601751026020', shelf_life=40)
        other_type = self._make_product(
            'OTHER', '04601751026021', shelf_life=14,
            type_formation=TypeFormationUIP.party_beginning,
        )
        inactive = self._make_product(
            'OFF', '04601751026022', shelf_life=14, is_active=False,
        )

        result = accumulate_short_shelf_life_reserve(pause_seconds=0)

        self.assertEqual(UIP.objects.filter(product_sku=long_sku).count(), 0)
        self.assertEqual(UIP.objects.filter(product_sku=other_type).count(), 0)
        self.assertEqual(UIP.objects.filter(product_sku=inactive).count(), 0)
        self.assertEqual(result['created'], 3)
        self.assertEqual(UIP.objects.filter(product_sku=self.sku).count(), 3)

    def test_existing_active_skipped_and_deleted_restored(self):
        active_date = self.today + timedelta(days=1)
        UIP.objects.create(
            product_sku=self.sku,
            number=self._number_for(active_date),
            status=PartyStatusChoices.RESERVED_LOCAL,
            production_date=active_date,
            reservation_date=self.today,
        )

        deleted_date = self.today + timedelta(days=2)
        burned = UIP.objects.create(
            product_sku=self.sku,
            number=self._number_for(deleted_date),
            status=PartyStatusChoices.DELETED,
            production_date=deleted_date,
            reservation_date=self.today - timedelta(days=30),
        )

        with patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_reserve.return_value = self._cz_success()
            result = accumulate_short_shelf_life_reserve(
                pause_seconds=0, skip_cz=False
            )

        self.assertEqual(result['created'], 1)  # только сегодня
        self.assertEqual(result['skipped_existing'], 1)
        self.assertEqual(result['restored'], 1)
        burned.refresh_from_db()
        self.assertEqual(burned.status, PartyStatusChoices.RESERVED_LOCAL)
        self.assertEqual(burned.reservation_date, self.today)

    def test_draft_mode_does_not_restore_deleted(self):
        deleted_date = self.today + timedelta(days=2)
        burned = UIP.objects.create(
            product_sku=self.sku,
            number=self._number_for(deleted_date),
            status=PartyStatusChoices.DELETED,
            production_date=deleted_date,
            reservation_date=self.today - timedelta(days=30),
        )

        result = accumulate_short_shelf_life_reserve(pause_seconds=0)

        self.assertEqual(result['restored'], 0)
        burned.refresh_from_db()
        self.assertEqual(burned.status, PartyStatusChoices.DELETED)

    def test_skips_when_reserve_above_threshold_in_cz_mode(self):
        with patch(
            'app_uip.services.reserve_accumulation.get_reserve_stats'
        ) as mock_stats, patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve:
            mock_stats.return_value = {'count': 9500, 'limit': 10000, 'percent': 95.0}
            result = accumulate_short_shelf_life_reserve(
                pause_seconds=0, skip_cz=False
            )

        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'reserve_full')
        mock_reserve.assert_not_called()
        self.assertEqual(UIP.objects.count(), 0)

    def test_draft_mode_ignores_reserve_threshold(self):
        with patch(
            'app_uip.services.reserve_accumulation.get_reserve_stats'
        ) as mock_stats:
            mock_stats.return_value = {'count': 9500, 'limit': 10000, 'percent': 95.0}
            result = accumulate_short_shelf_life_reserve(pause_seconds=0)

        self.assertFalse(result['skipped'])
        self.assertEqual(result['created'], 3)

    def test_cz_numbers_are_reserved_in_batches(self):
        from app_uip.services import reserve_accumulation

        entries = [
            {'sku': self.sku, 'number': f'number-{i}', 'action': 'create'}
            for i in range(3)
        ]
        with patch.object(reserve_accumulation, 'CZ_BATCH_SIZE', 2), patch(
            'app_uip.services.reserve_accumulation.reserve_parties_honest_sign'
        ) as mock_reserve, patch(
            'app_uip.services.reserve_accumulation.time.sleep'
        ) as mock_sleep:
            mock_reserve.return_value = self._cz_success()
            reserved, errors = reserve_accumulation._reserve_numbers(
                entries, pause_seconds=10
            )

        self.assertEqual(mock_reserve.call_count, 2)  # 2 + 1
        self.assertEqual(len(reserved), 3)
        self.assertEqual(errors, [])
        # Пауза только между запросами (не перед первым).
        self.assertEqual(mock_sleep.call_count, 1)

