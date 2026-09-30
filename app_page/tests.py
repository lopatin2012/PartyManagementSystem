# app_page/tests.py

"""Страница контроля продукции и привязка учётной записи к заводу."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

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
    TypeFormationUIP,
    UserFactory,
    Workshop,
)

STATIC_OVERRIDE = override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})


def create_product(factory, name, article, gtin):
    """Продукт с SKU, упаковкой и местом производства на заводе."""
    workshop = Workshop.objects.create(factory=factory, name=f'Цех {name}')
    line = Line.objects.create(workshop=workshop, name=f'Линия {name}')
    product = Product.objects.create(
        group=ProductGroupChoices.MILK,
        name=name,
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
    ProductProductionLocation.objects.create(product_sku=sku, line=line)
    return product, sku


@STATIC_OVERRIDE
class ProductControlViewTests(TestCase):
    """Доступ, поиск и редактирование на странице контроля продукции."""

    def setUp(self):
        # Заглушаем виджет статусов, чтобы не ходить в сеть.
        for patcher in (
            patch(
                'config.context_processors.check_factories',
                return_value={'is_ok': True, 'factories': []},
            ),
            patch(
                'app_helper.service_helper.diagnose_service',
                return_value={'is_available': True, 'checks': {'summary': {'ok': True}}},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.factory_a = Factory.objects.create(name='Завод A')
        self.factory_b = Factory.objects.create(name='Завод B')
        self.product_a, self.sku_a = create_product(
            self.factory_a, 'Продукт A', 'A-1', '04601751026011',
        )
        self.product_b, self.sku_b = create_product(
            self.factory_b, 'Продукт B', 'B-1', '04601751026012',
        )

        self.user_all = User.objects.create_user(
            username='all', password='pass',
        )
        self.user_a = User.objects.create_user(
            username='factory-a', password='pass',
        )
        UserFactory.objects.create(user=self.user_a, factory=self.factory_a)

    def test_login_required(self):
        response = self.client.get(reverse('product_control'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login/', response.url)

    def test_unbound_user_sees_all_products(self):
        self.client.force_login(self.user_all)

        response = self.client.get(reverse('product_control'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Продукт A')
        self.assertContains(response, 'Продукт B')

    def test_bound_user_sees_only_own_factory(self):
        self.client.force_login(self.user_a)

        response = self.client.get(reverse('product_control'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Продукт A')
        self.assertNotContains(response, 'Продукт B')

    def test_bound_user_cannot_open_other_factory_product(self):
        self.client.force_login(self.user_a)

        response = self.client.get(
            reverse('product_control_detail', args=[self.product_b.id]),
        )

        self.assertEqual(response.status_code, 404)

    def test_detail_renders_related_data(self):
        self.client.force_login(self.user_all)

        response = self.client.get(
            reverse('product_control_detail', args=[self.product_a.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A-1')
        self.assertContains(response, '04601751026011')
        self.assertContains(response, 'Линия Продукт A')

    def test_search_by_article(self):
        self.client.force_login(self.user_all)

        response = self.client.get(reverse('product_control'), {'q': 'B-1'})

        self.assertContains(response, 'Продукт B')
        self.assertNotContains(response, 'Продукт A')

    def test_search_by_gtin(self):
        self.client.force_login(self.user_all)

        response = self.client.get(
            reverse('product_control'), {'q': '04601751026012'},
        )

        self.assertContains(response, 'Продукт B')
        self.assertNotContains(response, 'Продукт A')

    def test_update_sku_fields(self):
        self.client.force_login(self.user_all)

        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {
                'target': 'sku',
                'target_id': str(self.sku_a.id),
                'type_formation_uip': TypeFormationUIP.party_end.value,
                'reserve_days': 9,
                'is_active': '1',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.sku_a.refresh_from_db()
        self.assertEqual(self.sku_a.type_formation_uip, TypeFormationUIP.party_end)
        self.assertEqual(self.sku_a.reserve_days, 9)
        self.assertTrue(self.sku_a.is_active)

    def test_update_product_active_flag(self):
        self.client.force_login(self.user_all)

        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {'target': 'product'},
        )

        self.assertEqual(response.status_code, 302)
        self.product_a.refresh_from_db()
        self.assertFalse(self.product_a.is_active)

    def test_card_states_are_readonly(self):
        self.client.force_login(self.user_all)

        response = self.client.get(
            reverse('product_control_detail', args=[self.product_a.id]),
        )

        self.assertContains(response, 'Состояние товара')
        self.assertNotContains(response, 'name="item_condition"')
        self.assertNotContains(response, 'name="card_status"')

    def test_product_states_not_changed_by_post(self):
        self.client.force_login(self.user_all)

        self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {
                'target': 'product',
                'is_active': '1',
                'item_condition': StateConditionChoices.NOT_READY_ORDER_KM,
                'card_status': CardStateChoices.DRAFT,
            },
        )

        self.product_a.refresh_from_db()
        self.assertEqual(
            self.product_a.item_condition, StateConditionChoices.READY_ORDER_KM,
        )
        self.assertEqual(
            self.product_a.card_status, CardStateChoices.PUBLISHED,
        )

    def test_update_rejects_foreign_sku(self):
        self.client.force_login(self.user_a)

        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {
                'target': 'sku',
                'target_id': str(self.sku_b.id),
                'type_formation_uip': TypeFormationUIP.natura.value,
            },
        )

        self.assertEqual(response.status_code, 404)
