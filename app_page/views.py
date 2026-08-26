# app_page/views.py

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.views import View
from django.views.generic import TemplateView

from app_cz.models import CISCode
from app_cz.services.party_service import get_available_products

from app_uip.models import UIP, PartyStatusChoices

from app_helper.search_helper import detect_search_type


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
