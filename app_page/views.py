# app_page/views.py

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from app_cz.models import CISCode, CISCodeArchive
from app_cz.services.party_service import get_available_products

from app_factory.models import (
    Factory,
    PackagingLevelChoices,
    Product,
    ProductGroupChoices,
    ProductPackaging,
    ProductProductionLocation,
    ProductSKU,
    TypeFormationUIP,
)

from app_uip.models import UIP, ProductionParty, PartyStatusChoices

from app_helper.access import UipPageAccessMixin, get_user_factory
from app_helper.search_helper import (
    detect_search_type,
    filter_codes_by_query,
    filter_uips_by_query,
)


class MainPageView(TemplateView):
    """Главная страница."""
    template_name = 'main/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context.update(
            {
                'title_name': 'Система управления партиями',
                'page_name': 'Главная страница',
            }
        )

        return context


class SearchView(View):
    """Универсальный поиск с автоматическим определением типа."""

    template_name = 'search/main.html'

    def get(self, request):
        query = request.GET.get('q', '').strip()

        # Определяем тип поиска автоматически.
        search_type = detect_search_type(query) if query else 'code'

        context = {
            'search_type': search_type,
            'query': query,
            'results': [],
            'total_count': 0,
        }

        if not query:
            return render(request, self.template_name, context)

        # Выполняем поиск в зависимости от определённого типа.
        in_archive = False
        if search_type == 'code':
            results = self._search_codes(query)
            if not results.exists():
                # Fallback: код не найден в рабочей таблице — ищем в архиве.
                results = self._search_archived_codes(query)
                in_archive = results.exists()
        elif search_type == 'uip':
            results = self._search_uip(query)
        else:
            results = []

        # Пагинация
        paginator = Paginator(results, 25)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)

        context.update({
            'results': page_obj,
            'total_count': paginator.count,
            'page_obj': page_obj,
            'in_archive': in_archive,
        })

        return render(request, self.template_name, context)

    def _search_codes(self, query):
        """Поиск по кодам маркировки (сначала точное совпадение по индексу)."""

        return filter_codes_by_query(
            CISCode.objects.all(), query
        ).select_related(
            'production_party__uip',
            'production_party__line__workshop__factory',
            'product_packaging__product'
        ).prefetch_related(
            'product_packaging__product__skus'
        ).order_by('-created_at')

    def _search_archived_codes(self, query):
        """Поиск по кодам в архиве (денормализованная таблица)."""
        return filter_codes_by_query(
            CISCodeArchive.objects.using('archive').all(), query
        ).order_by('-created_at')

    def _search_uip(self, query):
        """Поиск по УИП (сначала точное совпадение по индексу)."""
        return filter_uips_by_query(
            UIP.objects.all(), query
        ).select_related(
            'product_sku__product'
        ).prefetch_related(
            'production_parties__line__workshop__factory'
        ).order_by('-created_at')


class UIPListView(UipPageAccessMixin, TemplateView):
    """Страница со списком УИП с фильтрацией и пагинацией."""
    template_name = 'uip/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        get = self.request.GET

        # === Читаем все фильтры из query-параметров ===
        status_filter = get.get('status', '')
        number_filter = get.get('number', '').strip()
        article_filter = get.get('article', '').strip()
        prod_from = get.get('prod_from', '')
        prod_to = get.get('prod_to', '')
        res_from = get.get('res_from', '')
        res_to = get.get('res_to', '')
        # Показать только УИП в рассинхроне (is_desync=True).
        desync_only = get.get('desync') == '1'

        # === Базовый queryset ===
        # has_task — есть ли у УИП привязанное задание (для кнопки отчёта).
        queryset = UIP.objects.select_related(
            'product_sku__product'
        ).annotate(
            has_task=Exists(
                ProductionParty.objects.filter(uip=OuterRef('pk'))
            )
        ).order_by('-created_at')

        # === Применяем фильтры ===
        if desync_only:
            queryset = queryset.filter(is_desync=True)
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        if number_filter:
            queryset = queryset.filter(number__icontains=number_filter)
        if article_filter:
            queryset = queryset.filter(product_sku__article__icontains=article_filter)
        if prod_from:
            queryset = queryset.filter(production_date__gte=prod_from)
        if prod_to:
            queryset = queryset.filter(production_date__lte=prod_to)
        if res_from:
            queryset = queryset.filter(reservation_date__gte=res_from)
        if res_to:
            queryset = queryset.filter(reservation_date__lte=res_to)

        # === Количество зарезервированных УИП (без пагинации, по текущему фильтру) ===
        reserved_count = queryset.filter(
            status__in=[
                PartyStatusChoices.RESERVED_CZ,
                PartyStatusChoices.RESERVED_LOCAL,
            ]
        ).count()

        # === Пагинация: 100 записей на страницу ===
        paginator = Paginator(queryset, 100)
        page_number = get.get('page', 1)
        page_obj = paginator.get_page(page_number)

        # Диапазон страниц для пагинатора.
        current_page = page_obj.number
        total_pages = paginator.num_pages
        page_range = [1]
        start = max(2, current_page - 2)
        end = min(total_pages - 1, current_page + 2)
        if start > 2:
            page_range.append('...')
        page_range.extend(range(start, end + 1))
        if end < total_pages - 1:
            page_range.append('...')
        if total_pages > 1:
            page_range.append(total_pages)

        # Query string без page — для сохранения фильтров при переходе по страницам.
        params = get.copy()
        params.pop('page', None)
        query_string = params.urlencode()

        # Границы отображаемых записей.
        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        # Есть ли активные фильтры (для кнопки сброса и подсветки заголовков).
        has_active_filters = bool(
            desync_only
            or (status_filter and status_filter != 'all')
            or number_filter or article_filter
            or prod_from or prod_to or res_from or res_to
        )

        context.update({
            'title_name': 'Список УИП',
            'page_name': 'Список УИП',
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'reserved_count': reserved_count,
            'query_string': query_string,
            'start_item': start_item,
            'end_item': end_item,
            'available_products': get_available_products(),

            # Текущие значения фильтров (для сохранения в шаблоне).
            'current_status': status_filter,
            'current_number': number_filter,
            'current_article': article_filter,
            'current_prod_from': prod_from,
            'current_prod_to': prod_to,
            'current_res_from': res_from,
            'current_res_to': res_to,
            'desync_only': desync_only,
            'has_active_filters': has_active_filters,

            # Список статусов для выпадающего списка.
            'status_choices': PartyStatusChoices.choices,
        })

        return context


# ==========================================
# Контроль продукции.
# ==========================================

def _products_for_user(user):
    """
    Продукты, доступные пользователю для контроля.

    Если у учётной записи есть привязка к заводу — только продукция этого
    завода (по местам производства SKU → линия → цех → завод), иначе — все
    продукты всех заводов.
    """
    qs = Product.objects.all()
    factory = get_user_factory(user)
    if factory:
        qs = qs.filter(
            skus__product_production_locations__line__workshop__factory=factory
        ).distinct()
    return qs


def _to_bool(value) -> bool:
    """HTML-чекбокс/строка → bool."""
    return str(value).lower() in ('1', 'true', 'on', 'yes')


class ProductControlView(LoginRequiredMixin, TemplateView):
    """Страница контроля продукции: поиск продукта и его связей."""
    template_name = 'products/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        get = self.request.GET

        query = get.get('q', '').strip()
        group = get.get('group', '')
        active = get.get('active', '')
        factory_id = get.get('factory', '')
        scoped_factory = get_user_factory(self.request.user)

        queryset = _products_for_user(self.request.user)

        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(skus__article__icontains=query)
                | Q(packagings__gtin__icontains=query)
            ).distinct()
        if group:
            queryset = queryset.filter(group=group)
        if active in ('1', '0'):
            queryset = queryset.filter(is_active=(active == '1'))
        if factory_id:
            queryset = queryset.filter(
                skus__product_production_locations__line__workshop__factory_id=factory_id
            ).distinct()

        queryset = queryset.prefetch_related('skus', 'packagings').order_by('name')

        paginator = Paginator(queryset, 50)
        page_obj = paginator.get_page(get.get('page', 1))

        params = get.copy()
        params.pop('page', None)

        context.update({
            'title_name': 'Контроль продукции',
            'page_name': 'Контроль продукции',
            'page_obj': page_obj,
            'total_count': paginator.count,
            'query_string': params.urlencode(),
            'query': query,
            'current_group': group,
            'current_active': active,
            'current_factory': factory_id,
            'group_choices': ProductGroupChoices.choices,
            'scoped_factory': scoped_factory,
            'factories': (
                [] if scoped_factory
                else Factory.objects.order_by('name')
            ),
        })
        return context


class ProductControlDetailView(LoginRequiredMixin, View):
    """Карточка продукта: все связанные данные и их корректировка."""
    template_name = 'products/detail.html'

    def _get_product(self, request, pk):
        return get_object_or_404(_products_for_user(request.user), pk=pk)

    def get(self, request, pk):
        product = self._get_product(request, pk)
        return render(request, self.template_name, self._context(product))

    def post(self, request, pk):
        product = self._get_product(request, pk)
        try:
            self._apply(request, product)
            messages.success(request, 'Изменения сохранены.')
        except Http404:
            # Объект вне продукта/вне зоны видимости — отдаём 404 как есть.
            raise
        except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
            messages.error(request, f'Не удалось сохранить: {exc}')
        return redirect('product_control_detail', pk=product.pk)

    def _apply(self, request, product):
        target = request.POST.get('target')
        target_id = request.POST.get('target_id')

        if target == 'product':
            # Состояние товара/карточки задаётся синхронизацией с НК/ЧЗ —
            # вручную не редактируется, меняем только активность.
            product.is_active = _to_bool(request.POST.get('is_active'))
            product.save()

        elif target == 'sku':
            sku = get_object_or_404(ProductSKU, pk=target_id, product=product)
            sku.is_active = _to_bool(request.POST.get('is_active'))
            type_formation = request.POST.get('type_formation_uip')
            if type_formation:
                sku.type_formation_uip = int(type_formation)
            reserve_days = request.POST.get('reserve_days')
            if reserve_days not in (None, ''):
                sku.reserve_days = max(0, int(reserve_days))
            sku.save()

        elif target == 'packaging':
            packaging = get_object_or_404(
                ProductPackaging, pk=target_id, product=product,
            )
            packaging.is_active = _to_bool(request.POST.get('is_active'))
            for field in ('quantity_inside', 'code_storage_period_in_days'):
                value = request.POST.get(field)
                if value not in (None, ''):
                    setattr(packaging, field, int(value))
            if 'code_tnved' in request.POST:
                packaging.code_tnved = (
                    request.POST.get('code_tnved') or ''
                ).strip() or None
            packaging.save()

        elif target == 'location':
            location = get_object_or_404(
                ProductProductionLocation,
                pk=target_id,
                product_sku__product=product,
            )
            location.is_active = _to_bool(request.POST.get('is_active'))
            location.save()

        else:
            raise ValueError('Неизвестный объект редактирования.')

    def _context(self, product):
        skus = list(product.skus.all())
        locations = (
            ProductProductionLocation.objects
            .filter(product_sku__in=skus)
            .select_related('line__workshop__factory')
            .order_by('line__name')
        )
        locations_by_sku = {}
        for location in locations:
            locations_by_sku.setdefault(location.product_sku_id, []).append(location)

        sku_rows = [
            {'sku': sku, 'locations': locations_by_sku.get(sku.id, [])}
            for sku in skus
        ]

        return {
            'title_name': product.name,
            'page_name': 'Контроль продукции',
            'product': product,
            'sku_rows': sku_rows,
            'packagings': list(product.packagings.all()),
            'type_choices': TypeFormationUIP.choices,
            'level_choices': PackagingLevelChoices.choices,
        }
