from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from app_factory.models import (
    Product,
    ProductPackaging,
    ProductSKU,
    PackagingLevelChoices,
    ProductGroupChoices,
    StateConditionChoices,
    CardStateChoices,
)

from app_uip.models import UIP, PartyStatusChoices
from app_uip.serializers import (
    UIPReserveItemSerializer,
    UIPReserveRequestSerializer,
)
from app_uip.services.uip_reserve import reserve_uips


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