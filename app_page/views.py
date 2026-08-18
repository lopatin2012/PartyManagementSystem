# app_page/views.py

import json
import logging
import re
import threading
from datetime import datetime

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView
from django.shortcuts import render

from app_cz.models import CISCode, SUZAccount
from app_cz.services.party_service import sync_parties_from_cz, generate_uip, get_available_products
from app_cz.services.code_sync import (
    sync_codes_for_party,
    sync_all_external_tasks,
)
from app_factory.models import ProductSKU, NationalCatalogProduct, CardStateChoices, StateConditionChoices

from app_uip.models import (
    UIP, PartyStatusChoices,
    ProductionParty, ProductionPartyStatusChoices, ProductionPartySyncStatusChoices,
)

from app_helper.search_helper import detect_search_type


logger = logging.getLogger(__name__)

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
        if search_type == 'code':
            results = self._search_codes(query)
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
        })

        return render(request, self.template_name, context)

    def _search_codes(self, query):
        """Поиск по кодам маркировки."""

        return CISCode.objects.filter(
            Q(code__iexact=query) | Q(code__istartswith=query)
        ).select_related(
            'production_party__uip',
            'product_packaging__product'
        ).order_by('-created_at')

    def _search_uip(self, query):
        """Поиск по УИП."""
        return UIP.objects.filter(
            number__iexact=query
        ).select_related(
            'product_sku__product'
        ).order_by('-created_at')


class UIPListView(TemplateView):
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

        # === Базовый queryset ===
        queryset = UIP.objects.select_related(
            'product_sku__product'
        ).order_by('-created_at')

        # === Применяем фильтры ===
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
            (status_filter and status_filter != 'all')
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
            'has_active_filters': has_active_filters,

            # Список статусов для выпадающего списка.
            'status_choices': PartyStatusChoices.choices,
        })

        return context


@method_decorator(staff_member_required, name='dispatch')
class SyncPartiesView(View):
    """Синхронизация УИП из Честного Знака (только для админов)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse({
                'is_error': True,
                'message': 'Доступ только для администраторов'
            }, status=403)

        result = sync_parties_from_cz()

        status_code = (
            502
            if result.get('is_error')
            else 200
        )
        return JsonResponse(result, status=status_code)

@method_decorator(staff_member_required, name='dispatch')
class GenerateUIPView(View):
    """Генерация УИП вручную (только для администраторов)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Доступ только для администраторов.'
                },
                status=403
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Некорректный формат данных.'
                },
                status=400
            )

        product_sku_id = data.get('product_sku_id')
        production_date_str = data.get('production_date')
        mode = data.get('mode', 'local')
        party = data.get('party') or '000'

        if not product_sku_id or not production_date_str:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Не указаны обязательные параметры.'
                },
                status=400
            )

        try:
            production_date = datetime.strptime(
                production_date_str, '%Y-%m-%d'
            ).date()
        except ValueError:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Некорректный формат даты (ожидается ГГГГ-ММ-ДД).'
                },
                status=400
            )

        # Получаем объект продукта из БД.
        try:
            product_sku = ProductSKU.objects.get(id=product_sku_id)

        except ProductSKU.DoesNotExist:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Не найден указанный продукт по id'
                },
                status=400
            )

        result = generate_uip(
            product_sku, production_date, mode,
            party=party
        )

        status_code = (
            200
            if not result.get('is_error')
            else 400
        )
        return JsonResponse(result, status=status_code)


# ==========================================
# Страница синхронизации с внешним сервисом (Молвест.Маркировка).
# ==========================================

@method_decorator(staff_member_required, name='dispatch')
class SyncTasksView(TemplateView):
    """
    Страница отслеживания синхронизации заданий с внешним сервисом.

    Показывает производственные партии, полученные из внешнего сервиса:
    статус задания, статус синхронизации, последнюю синхронизацию и т.д.
    """
    template_name = 'sync/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        get = self.request.GET

        status_filter = get.get('status', '')
        sync_filter = get.get('sync', '')
        search = get.get('search', '').strip()

        queryset = ProductionParty.objects.filter(
            is_external=True
        ).select_related(
            'uip__product_sku__product',
            'line__workshop__factory',
        ).order_by('-created_at')

        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        if sync_filter and sync_filter != 'all':
            queryset = queryset.filter(sync_status=sync_filter)
        if search:
            queryset = queryset.filter(
                Q(external_number_task__icontains=search)
                | Q(production_party__icontains=search)
                | Q(uip__number__icontains=search)
                | Q(uip__product_sku__article__icontains=search)
            )

        paginator = Paginator(queryset, 50)
        page_number = get.get('page', 1)
        page_obj = paginator.get_page(page_number)

        # Диапазон страниц.
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

        params = get.copy()
        params.pop('page', None)
        query_string = params.urlencode()

        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        # Статистика по статусам синхронизации.
        base_stats = ProductionParty.objects.filter(is_external=True)
        stats = {
            'total': base_stats.count(),
            'pending': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.PENDING
            ).count(),
            'synced': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.SYNCED
            ).count(),
            'error': base_stats.filter(
                sync_status=ProductionPartySyncStatusChoices.ERROR
            ).count(),
            'active': base_stats.filter(
                status__in=[
                    ProductionPartyStatusChoices.CREATED,
                    ProductionPartyStatusChoices.WORK,
                    ProductionPartyStatusChoices.CLOSED,
                ]
            ).count(),
        }

        has_active_filters = bool(
            (status_filter and status_filter != 'all')
            or (sync_filter and sync_filter != 'all')
            or search
        )

        context.update({
            'title_name': 'Синхронизация заданий',
            'page_name': 'Синхронизация заданий',
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'query_string': query_string,
            'start_item': start_item,
            'end_item': end_item,
            'stats': stats,
            'status_choices': ProductionPartyStatusChoices.choices,
            'sync_status_choices': ProductionPartySyncStatusChoices.choices,
            'current_status': status_filter,
            'current_sync': sync_filter,
            'current_search': search,
            'has_active_filters': has_active_filters,
        })

        return context


@method_decorator(staff_member_required, name='dispatch')
class SyncTaskCodesView(View):
    """
    Ручная синхронизация кодов для одной производственной партии.
    POST /sync/task-codes/  {production_party_id: uuid}
    """

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {'is_error': True, 'message': 'Некорректный формат данных.'},
                status=400
            )

        party_id = data.get('production_party_id')
        if not party_id:
            return JsonResponse(
                {'is_error': True, 'message': 'Не указан production_party_id.'},
                status=400
            )

        try:
            party = ProductionParty.objects.get(id=party_id)
        except (ProductionParty.DoesNotExist, ValueError, TypeError):
            return JsonResponse(
                {'is_error': True, 'message': 'Производственная партия не найдена.'},
                status=400
            )

        result = sync_codes_for_party(party)

        status_code = 400 if result.get('has_error') else 200
        return JsonResponse(result, status=status_code)


@method_decorator(staff_member_required, name='dispatch')
class SyncAllTasksView(View):
    """
    Запуск полной синхронизации с внешним сервисом вручную.
    POST /sync/all/
    """

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403
            )

        result = sync_all_external_tasks()

        status_code = 502 if result.get('is_error') else 200
        return JsonResponse(result, status=status_code)


# ==========================================
# Национальный каталог (ГИС МТ).
# ==========================================

# Хранилище хода синхронизации НК: {user_id: progress}.
_NK_SYNC_PROGRESS = {}
_NK_SYNC_LOCK = threading.Lock()


def _nk_progress(user_id: int) -> dict:
    """Возвращает словарь прогресса для пользователя."""
    with _NK_SYNC_LOCK:
        return _NK_SYNC_PROGRESS.get(user_id, {
            'running': False, 'total': 0, 'done': 0,
            'created': 0, 'updated': 0, 'current_name': '', 'error': None,
        })


@method_decorator(staff_member_required, name='dispatch')
class NationalCatalogView(TemplateView):
    """Страница Национального каталога (только для администраторов)."""
    template_name = 'nk/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        get = self.request.GET
        q = get.get('q', '').strip()
        gtin_filter = get.get('gtin', '').strip()
        product_group = get.get('product_group', '').strip()
        readiness = get.get('readiness', '').strip()
        card_state = get.get('card_state', '').strip()
        state_condition = get.get('state_condition', '').strip()
        created_from = get.get('created_from', '').strip()
        created_to = get.get('created_to', '').strip()

        products = NationalCatalogProduct.objects.order_by('-synced_at')
        if q:
            products = products.filter(name__iregex=re.escape(q))
        if gtin_filter:
            products = products.filter(gtin__icontains=gtin_filter)
        if product_group:
            products = products.filter(product_group=product_group)
        if card_state:
            products = products.filter(card_state=card_state)
        if state_condition:
            products = products.filter(state_condition=state_condition)
        if created_from:
            products = products.filter(create_date__date__gte=created_from)
        if created_to:
            products = products.filter(create_date__date__lte=created_to)
        if readiness == 'ready':
            products = products.filter(
                card_state='published',
                state_condition__in=('ready_order_km', 'ready_commercialization'),
            )
        elif readiness == 'not_ready':
            products = products.exclude(
                card_state='published',
                state_condition__in=('ready_order_km', 'ready_commercialization'),
            )

        product_groups = (
            NationalCatalogProduct.objects
            .exclude(product_group='')
            .order_by('product_group_name')
            .values_list('product_group', 'product_group_name')
            .distinct()
        )

        paginator = Paginator(products, 25)
        page_number = get.get('page', 1)
        try:
            page_obj = paginator.get_page(page_number)
        except Exception:
            page_obj = paginator.get_page(1)

        # Диапазон страниц.
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

        params = get.copy()
        params.pop('page', None)
        query_string = params.urlencode()

        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        has_active_filters = bool(
            q or gtin_filter or product_group or card_state
            or state_condition or created_from or created_to or readiness
        )

        context.update({
            'title_name': 'Национальный каталог',
            'page_name': 'Национальный каталог',
            'products': page_obj.object_list,
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'query_string': query_string,
            'start_item': start_item,
            'end_item': end_item,
            'product_groups': product_groups,
            'card_state_choices': CardStateChoices.choices,
            'state_condition_choices': StateConditionChoices.choices,
            'default_inn': SUZAccount.objects.filter(is_active=True).values_list('inn', flat=True).first() or '',
            'q': q,
            'current_gtin': gtin_filter,
            'current_product_group': product_group,
            'current_readiness': readiness,
            'current_card_state': card_state,
            'current_state_condition': state_condition,
            'current_created_from': created_from,
            'current_created_to': created_to,
            'has_active_filters': has_active_filters,
        })

        return context


@method_decorator(staff_member_required, name='dispatch')
class NKSyncProductsView(View):
    """API: Запуск синхронизации товаров Национального каталога (POST)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403,
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = {}

        owner_inn = data.get('owner_inn') or None
        brand_id = data.get('brand_id') or None
        cat_id = data.get('cat_id') or None

        with _NK_SYNC_LOCK:
            existing = _NK_SYNC_PROGRESS.get(request.user.id)
            if existing and existing.get('running'):
                return JsonResponse(
                    {'is_error': True, 'message': 'Синхронизация уже выполняется'},
                    status=409,
                )

        progress = {
            'running': True, 'total': 0, 'done': 0,
            'created': 0, 'updated': 0, 'current_name': '', 'error': None,
            'phase': 'list',
        }
        with _NK_SYNC_LOCK:
            _NK_SYNC_PROGRESS[request.user.id] = progress

        def _run_sync():
            from app_cz.services.national_catalog_client import sync_products
            from app_factory.services.nk_sync_service import sync_nk_to_products
            try:
                sync_products(
                    request.user,
                    owner_inn=owner_inn,
                    brand_id=brand_id,
                    cat_id=cat_id,
                    progress=progress,
                )
                # После выгрузки данных из НК — синхронизируем Product/ProductPackaging.
                product_progress = {
                    'total': 0, 'done': 0,
                    'products_created': 0, 'products_updated': 0,
                    'packagings_created': 0, 'skus_created': 0,
                }
                sync_nk_to_products(user=request.user, progress=product_progress)
                progress['product_sync'] = product_progress
            except Exception as e:
                logger.error(f'Ошибка синхронизации НК: {e}', exc_info=True)
                progress['error'] = str(e)
            finally:
                progress['running'] = False

        threading.Thread(target=_run_sync, daemon=True).start()

        return JsonResponse({'is_error': False, 'message': 'Синхронизация запущена'})


@method_decorator(staff_member_required, name='dispatch')
class NKSyncProgressView(View):
    """API: Ход выполнения синхронизации НК (GET)."""

    def get(self, request):
        return JsonResponse({
            'is_error': False,
            'progress': _nk_progress(request.user.id),
        })


@method_decorator(staff_member_required, name='dispatch')
class NKProductCreateView(View):
    """POST: ручное создание Product + ProductPackaging + ProductSKU
    из товара Национального каталога."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse({'is_error': True, 'message': 'Доступ только для администраторов'}, status=403)

        data = json.loads(request.body)
        nk_product_id = data.get('nk_product_id')
        if not nk_product_id:
            return JsonResponse({'is_error': True, 'message': 'Не указан ID товара НК'}, status=400)

        try:
            nk = NationalCatalogProduct.objects.get(id=nk_product_id)
        except NationalCatalogProduct.DoesNotExist:
            return JsonResponse({'is_error': True, 'message': 'Товар НК не найден'}, status=404)

        # Если уже создан — возвращаем существующий.
        existing = Product.objects.filter(national_product=nk, is_active=True).first()
        if existing:
            return JsonResponse({
                'is_error': False,
                'message': 'Продукт уже создан',
                'product_id': str(existing.id),
            })

        from app_factory.services.nk_sync_service import sync_nk_to_products
        result = sync_nk_to_products(user=request.user, progress=None)

        product = Product.objects.filter(national_product=nk, is_active=True).first()
        if product:
            return JsonResponse({
                'is_error': False,
                'message': 'Продукт создан из Национального каталога',
                'product_id': str(product.id),
            })

        return JsonResponse({
            'is_error': True,
            'message': 'Не удалось создать продукт (товар не готов к производству)',
        }, status=400)


class NKProductDetailView(View):
    """API: Получение товара по good_id или gtin (POST)."""

    def post(self, request):
        if not request.user.is_superuser:
            return JsonResponse(
                {'is_error': True, 'message': 'Доступ только для администраторов'},
                status=403,
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse(
                {'is_error': True, 'message': 'Некорректный формат данных'},
                status=400,
            )

        good_id = data.get('good_id') or None
        gtin = data.get('gtin') or None

        if not good_id and not gtin:
            return JsonResponse(
                {'is_error': True, 'message': 'Укажите good_id или gtin'},
                status=400,
            )

        if good_id:
            product = NationalCatalogProduct.objects.filter(good_id=good_id).first()
        else:
            product = NationalCatalogProduct.objects.filter(gtin=gtin).first()

        if product:
            return JsonResponse({
                'is_error': False,
                'source': 'db',
                'product': {
                    'good_id': product.good_id,
                    'gtin': product.gtin,
                    'name': product.name,
                    'brand_name': product.brand_name,
                    'product_group': product.product_group,
                    'product_group_name': product.product_group_name,
                    'card_state': product.card_state,
                    'card_state_name': product.get_card_state_display(),
                    'state_condition': product.state_condition,
                    'state_condition_name': product.get_state_condition_display(),
                    'is_ready_for_production': product.is_ready_for_production,
                    'image_url': product.image_url,
                    'synced_at': product.synced_at.isoformat(),
                },
            })

        # Если нет в локальном кэше — запрашиваем напрямую из НК.
        try:
            from app_cz.services.national_catalog_client import NationalCatalogClient
            client = NationalCatalogClient(request.user)
            items = client.get_product(
                good_id=int(good_id) if good_id else None,
                gtin=gtin,
            )
            return JsonResponse({
                'is_error': False,
                'source': 'nk',
                'product': items[0] if items else None,
            })
        except Exception as e:
            logger.error(f'Ошибка получения товара НК: {e}', exc_info=True)
            return JsonResponse(
                {'is_error': True, 'message': f'Ошибка запроса к НК: {e}'},
                status=500,
            )
