# app_page/tests.py

"""Страница контроля продукции и привязка учётной записи к заводу."""

from unittest.mock import patch

from django.contrib.auth.models import Group, User
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



@STATIC_OVERRIDE
class UipListViewDesyncFilterTests(TestCase):
    """Фильтр «В рассинхроне» на странице /uip/."""

    def setUp(self):
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

        self.admin = User.objects.create_superuser(
            username='admin-uip', password='pass', email='a@a.a',
        )
        self.client.force_login(self.admin)

    def _uip(self, number, is_desync):
        from app_factory.models import ProductSKU
        from app_uip.models import UIP, PartyStatusChoices

        product = Product.objects.create(
            group=ProductGroupChoices.MILK,
            name=f'P {number}',
            shelf_life_in_days=14,
            item_condition=StateConditionChoices.READY_ORDER_KM,
            card_status=CardStateChoices.PUBLISHED,
        )
        ProductPackaging.objects.create(
            product=product,
            level=PackagingLevelChoices.UNIT,
            gtin=f'0460175102{number}',
            quantity_inside=1,
        )
        sku = ProductSKU.objects.create(product=product, article=number)
        return UIP.objects.create(
            product_sku=sku,
            number=f'0460175102{number}00101500320000000',
            status=PartyStatusChoices.RESERVED_LOCAL,
            is_desync=is_desync,
        )

    def test_desync_filter_shows_only_desync(self):
        self._uip('11', True)
        self._uip('12', False)

        response = self.client.get(reverse('uip_list') + '?desync=1')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 1)
        self.assertTrue(response.context['desync_only'])

    def test_without_filter_shows_all(self):
        self._uip('13', True)
        self._uip('14', False)

        response = self.client.get(reverse('uip_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 2)
        self.assertFalse(response.context['desync_only'])


@STATIC_OVERRIDE
class NavigationUipLinkTests(TestCase):
    """Ссылка «УИП» в навигации видна только тем, кому доступна страница."""

    def setUp(self):
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

    def test_anonymous_has_no_uip_link(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'href="/uip/"')

    def test_admin_has_uip_link(self):
        self.client.force_login(
            User.objects.create_superuser(
                username='admin-nav', password='pass', email='a@a.a',
            )
        )

        response = self.client.get(reverse('home'))

        self.assertContains(response, 'href="/uip/"')

    def test_authenticated_without_access_has_no_uip_link(self):
        self.client.force_login(
            User.objects.create_user(username='plain-nav', password='pass')
        )

        response = self.client.get(reverse('home'))

        self.assertNotContains(response, 'href="/uip/"')


@STATIC_OVERRIDE
class FactoryScopedUipVisibilityTests(TestCase):
    """УИП видны только в пределах завода пользователя (админы — все)."""

    def setUp(self):
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

        from app_uip.models import UIP, PartyStatusChoices

        self.factory_a = Factory.objects.create(name='Завод A')
        self.factory_b = Factory.objects.create(name='Завод B')
        self.product_a, self.sku_a = create_product(
            self.factory_a, 'Продукт A', 'A-1', '04601751026011',
        )
        self.product_b, self.sku_b = create_product(
            self.factory_b, 'Продукт B', 'B-1', '04601751026012',
        )

        self.uip_a = UIP.objects.create(
            product_sku=self.sku_a,
            number='04601751026011001015003200000001',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )
        self.uip_b = UIP.objects.create(
            product_sku=self.sku_b,
            number='04601751026012001015003200000002',
            status=PartyStatusChoices.RESERVED_LOCAL,
        )

        self.view_group = Group.objects.get_or_create(name='Просмотр')[0]
        self.user_a = User.objects.create_user(username='fa', password='pass')
        self.user_a.groups.add(self.view_group)
        UserFactory.objects.create(user=self.user_a, factory=self.factory_a)

        self.admin = User.objects.create_superuser(
            username='adm', password='pass', email='a@a.a',
        )
        UserFactory.objects.create(user=self.admin, factory=self.factory_a)

    def test_bound_user_sees_only_own_uips(self):
        self.client.force_login(self.user_a)

        response = self.client.get(reverse('uip_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 1)
        self.assertContains(response, self.uip_a.number)
        self.assertNotContains(response, self.uip_b.number)

    def test_generation_dropdown_scoped_to_factory(self):
        self.client.force_login(self.user_a)

        response = self.client.get(reverse('uip_list'))

        articles = {p['article'] for p in response.context['available_products']}
        self.assertEqual(articles, {'A-1'})

    def test_unbound_viewer_sees_all_uips(self):
        viewer = User.objects.create_user(username='viewer', password='pass')
        viewer.groups.add(self.view_group)
        self.client.force_login(viewer)

        response = self.client.get(reverse('uip_list'))

        self.assertEqual(response.context['total_count'], 2)

    def test_bound_admin_sees_all_uips(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('uip_list'))

        self.assertEqual(response.context['total_count'], 2)


@STATIC_OVERRIDE
class ProductDetailFactoryScopeTests(TestCase):
    """Карточка продукта: видны и редактируются только данные своего завода."""

    def setUp(self):
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

        # Тот же продукт производится и на заводе B (чужая линия + чужой SKU).
        workshop_b = Workshop.objects.create(factory=self.factory_b, name='Цех B')
        line_b = Line.objects.create(workshop=workshop_b, name='Линия B')
        self.location_b = ProductProductionLocation.objects.create(
            product_sku=self.sku_a, line=line_b,
        )
        self.sku_a2 = ProductSKU.objects.create(
            product=self.product_a, article='A-2',
        )
        ProductProductionLocation.objects.create(
            product_sku=self.sku_a2, line=line_b,
        )

        self.user_a = User.objects.create_user(username='fa', password='pass')
        UserFactory.objects.create(user=self.user_a, factory=self.factory_a)
        self.client.force_login(self.user_a)

    def test_detail_hides_other_factory_data(self):
        response = self.client.get(
            reverse('product_control_detail', args=[self.product_a.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A-1')
        self.assertNotContains(response, 'A-2')
        self.assertNotContains(response, 'Линия B')

    def test_cannot_edit_foreign_location(self):
        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {'target': 'location', 'target_id': str(self.location_b.id)},
        )

        self.assertEqual(response.status_code, 404)

    def test_cannot_edit_foreign_sku(self):
        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {'target': 'sku', 'target_id': str(self.sku_a2.id)},
        )

        self.assertEqual(response.status_code, 404)

    def test_can_edit_own_location(self):
        own_location = ProductProductionLocation.objects.get(
            product_sku=self.sku_a, line__workshop__factory=self.factory_a,
        )

        response = self.client.post(
            reverse('product_control_detail', args=[self.product_a.id]),
            {'target': 'location', 'target_id': str(own_location.id)},
        )

        self.assertEqual(response.status_code, 302)

    def test_bound_admin_sees_all_factory_data(self):
        admin = User.objects.create_superuser(
            username='adm-detail', password='pass', email='a@a.a',
        )
        # Привязан к заводу A, но должен видеть и чужие данные.
        UserFactory.objects.create(user=admin, factory=self.factory_a)
        self.client.force_login(admin)

        response = self.client.get(
            reverse('product_control_detail', args=[self.product_a.id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A-2')
        self.assertContains(response, 'Линия B')
