# app_factory/tests.py

"""
Тесты проверки активности продуктов через серверы «Молвест.Маркировка».

`sync_product_activity` запрашивает product-list с сервера каждого завода
и деактивирует SKU, отсутствующие в списке или помеченные `active=false`.
"""

from unittest.mock import patch

from django.test import TestCase

from app_factory.models import (
    CardStateChoices,
    Factory,
    Line,
    PackagingLevelChoices,
    Product,
    ProductGroupChoices,
    ProductPackaging,
    ProductProductionLocation,
    ProductSKU,
    StateConditionChoices,
    Workshop,
)
from app_factory.services.product_activity_sync import sync_product_activity


def create_sku_with_line(factory, article, gtin):
    """Создаёт продукт + SKU + потребительскую упаковку + место производства."""
    product = Product.objects.create(
        group=ProductGroupChoices.MILK,
        name=f'Продукт {article}',
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
    sku = ProductSKU.objects.create(product=product, article=article)
    ProductProductionLocation.objects.create(
        product_sku=sku,
        line=factory.workshop_set.first().line_set.first(),
    )
    return product, sku


class ProductActivitySyncTests(TestCase):
    def setUp(self):
        self.factory = Factory.objects.create(
            name='Тестовый завод',
            ip_address='127.0.0.1',
            port_address=8000,
            is_active=True,
        )
        workshop = Workshop.objects.create(factory=self.factory, name='Цех 1')
        Line.objects.create(workshop=workshop, name='Линия 1')

    def _sync(self, product_list):
        with patch(
            'app_factory.services.product_activity_sync.fetch_factory_products',
            return_value=product_list,
        ):
            return sync_product_activity()

    def test_activity_from_molvest_activates_and_deactivates(self):
        _, sku_active = create_sku_with_line(
            self.factory, 'A1', '04601751026011',
        )
        _, sku_disabled = create_sku_with_line(
            self.factory, 'A2', '04601751026012',
        )
        _, sku_missing = create_sku_with_line(
            self.factory, 'A3', '04601751026013',
        )
        # A3 выключен в СУП, но в Молвест активен — должен включиться.
        sku_missing.is_active = False
        sku_missing.save(update_fields=['is_active'])

        result = self._sync([
            {'code': 'A1', 'name': 'A1', 'gtin': '04601751026011',
             'uuid_str': str(sku_active.product_id), 'active': True},
            {'code': 'A2', 'name': 'A2', 'gtin': '04601751026012',
             'uuid_str': str(sku_disabled.product_id), 'active': False},
            {'code': 'A3', 'name': 'A3', 'gtin': '04601751026013',
             'uuid_str': str(sku_missing.product_id), 'active': True},
        ])

        self.assertFalse(result['is_error'])
        sku_active.refresh_from_db()
        sku_disabled.refresh_from_db()
        sku_missing.refresh_from_db()
        self.assertTrue(sku_active.is_active)
        self.assertFalse(sku_disabled.is_active)
        self.assertTrue(sku_missing.is_active)

    def test_sku_links_filled_per_factory(self):
        _, sku = create_sku_with_line(self.factory, 'L1', '04601751026051')

        self._sync([
            {'code': 'L1', 'name': 'L1', 'gtin': '04601751026051',
             'uuid_str': str(sku.product_id), 'active': True},
        ])

        sku.refresh_from_db()
        self.assertEqual(len(sku.sku_links), 1)
        link = sku.sku_links[0]
        self.assertEqual(link['system'], 'molvest')
        self.assertEqual(link['article'], 'L1')
        self.assertEqual(link['gtin'], '04601751026051')
        self.assertEqual(link['factory'], str(self.factory.id))
        self.assertTrue(link['active'])

    def test_creates_missing_sku_by_article(self):
        product, _ = create_sku_with_line(self.factory, 'N1', '04601751026061')

        self._sync([
            {'code': 'N1', 'name': 'N1', 'gtin': '04601751026061',
             'uuid_str': str(product.id), 'active': True},
            # Второй артикул того же продукта (пластинка сменилась).
            {'code': 'N1-1', 'name': 'N1 (новая)', 'gtin': '04601751026061',
             'uuid_str': str(product.id), 'active': True},
        ])

        self.assertTrue(
            ProductSKU.objects.filter(product=product, article='N1-1').exists()
        )

    def test_product_deactivated_when_no_active_sku(self):
        product, sku = create_sku_with_line(
            self.factory, 'B1', '04601751026021',
        )

        self._sync([
            {'code': 'B1', 'name': 'B1', 'gtin': '04601751026021',
             'uuid_str': str(product.id), 'active': False},
        ])

        product.refresh_from_db()
        sku.refresh_from_db()
        self.assertFalse(sku.is_active)
        self.assertFalse(product.is_active)

    def test_failed_factory_does_not_deactivate(self):
        _, sku = create_sku_with_line(self.factory, 'D1', '04601751026041')

        result = self._sync(None)  # сервер недоступен

        sku.refresh_from_db()
        self.assertTrue(result['is_error'])
        self.assertIn(str(self.factory.id), result['failed_factories'])
        self.assertTrue(sku.is_active)

    def test_live_uip_protects_sku_from_deactivation(self):
        from app_uip.models import UIP, PartyStatusChoices

        product, sku = create_sku_with_line(self.factory, 'P1', '04601751026071')
        UIP.objects.create(
            product_sku=sku,
            number='04601751026071260101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )

        result = self._sync([
            {'code': 'P1', 'name': 'P1', 'gtin': '04601751026071',
             'uuid_str': str(product.id), 'active': False},
        ])

        sku.refresh_from_db()
        product.refresh_from_db()
        self.assertTrue(sku.is_active)      # защищён живым УИП
        self.assertTrue(product.is_active)
        self.assertEqual(result['skus_protected'], 1)


class NKShelfLifeTests(TestCase):
    """Срок годности продукта берётся из атрибутов карточки НК."""

    def test_extract_shelf_life_days(self):
        from app_factory.services.nk_sync_service import _extract_shelf_life_days

        raw = {'good_attrs': [
            {'attr_name': 'Срок годности', 'attr_value': '180'},
        ]}
        self.assertEqual(_extract_shelf_life_days(raw), 180)

    def test_extract_shelf_life_missing_returns_none(self):
        from app_factory.services.nk_sync_service import _extract_shelf_life_days

        self.assertIsNone(_extract_shelf_life_days({'good_attrs': []}))
        self.assertIsNone(
            _extract_shelf_life_days(
                {'good_attrs': [{'attr_name': 'Что-то', 'attr_value': '5'}]}
            )
        )

    def test_extract_shelf_life_parses_prefixed_value(self):
        from app_factory.services.nk_sync_service import _extract_shelf_life_days

        raw = {'good_attrs': [
            {'attr_name': 'Срок годности', 'attr_value': '90 суток'},
        ]}
        self.assertEqual(_extract_shelf_life_days(raw), 90)
