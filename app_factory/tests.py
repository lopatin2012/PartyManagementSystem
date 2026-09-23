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

    def test_deactivates_missing_and_inactive_products(self):
        _, sku_active = create_sku_with_line(
            self.factory, 'A1', '04601751026011',
        )
        _, sku_disabled = create_sku_with_line(
            self.factory, 'A2', '04601751026012',
        )
        _, sku_missing = create_sku_with_line(
            self.factory, 'A3', '04601751026013',
        )

        product_list = [
            {'code': 'A1', 'name': 'Продукт A1', 'gtin': '04601751026011', 'active': True},
            {'code': 'A2', 'name': 'Продукт A2', 'gtin': '04601751026012', 'active': False},
            # A3 отсутствует
        ]

        with patch(
            'app_factory.services.product_activity_sync.fetch_factory_products',
            return_value=product_list,
        ):
            result = sync_product_activity()

        self.assertFalse(result['is_error'])
        self.assertEqual(result['deactivated'], 2)
        sku_active.refresh_from_db()
        sku_disabled.refresh_from_db()
        sku_missing.refresh_from_db()
        self.assertTrue(sku_active.is_active)
        self.assertFalse(sku_disabled.is_active)
        self.assertFalse(sku_missing.is_active)

    def test_product_deactivated_when_all_skus_inactive(self):
        product, sku = create_sku_with_line(
            self.factory, 'B1', '04601751026021',
        )

        with patch(
            'app_factory.services.product_activity_sync.fetch_factory_products',
            return_value=[],  # продукт пропал из списка завода
        ):
            sync_product_activity()

        product.refresh_from_db()
        sku.refresh_from_db()
        self.assertFalse(sku.is_active)
        self.assertFalse(product.is_active)

    def test_product_stays_active_if_other_sku_active(self):
        product, sku_one = create_sku_with_line(
            self.factory, 'C1', '04601751026031',
        )
        line = self.factory.workshop_set.first().line_set.first()
        sku_two = ProductSKU.objects.create(product=product, article='C2')
        ProductProductionLocation.objects.create(product_sku=sku_two, line=line)

        with patch(
            'app_factory.services.product_activity_sync.fetch_factory_products',
            return_value=[
                {'code': 'C2', 'name': 'Продукт C2', 'active': True},
            ],
        ):
            sync_product_activity()

        product.refresh_from_db()
        sku_one.refresh_from_db()
        sku_two.refresh_from_db()
        self.assertFalse(sku_one.is_active)
        self.assertTrue(sku_two.is_active)
        self.assertTrue(product.is_active)

    def test_failed_factory_does_not_deactivate(self):
        _, sku = create_sku_with_line(self.factory, 'D1', '04601751026041')

        with patch(
            'app_factory.services.product_activity_sync.fetch_factory_products',
            return_value=None,  # сервер недоступен
        ):
            result = sync_product_activity()

        sku.refresh_from_db()
        self.assertTrue(result['is_error'])
        self.assertIn(str(self.factory.id), result['failed_factories'])
        self.assertTrue(sku.is_active)
