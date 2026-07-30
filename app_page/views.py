# app_page/views.py

import logging
import json
from datetime import datetime

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django.shortcuts import render
from django.views import View
from django.core.paginator import Paginator
from django.db.models import Q

from app_cz.models import CISCode
from app_cz.services.party_service import sync_parties_from_cz, generate_uip, get_available_products

from app_uip.models import UIP, ProductionParty

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
    """Страница со списком последних 100 УИПов."""
    template_name = 'uip/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Получаем фильтр по статусу из query-параметров.
        status_filter = self.request.GET.get('status', 'all')

        # Базовый queryset.
        queryset = UIP.objects.select_related(
            'product_sku__product'
        ).order_by('-created_at')

        # Применяем фильтр по статусу.
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)

        # Пагинация: 100 записей на страницу.
        paginator = Paginator(queryset, 100)
        page_number = self.request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)

        # Формируем диапазон страниц для отображения в пагинаторе.
        # Показываем текущую страницу ± 2, плюс первую и последнюю.
        current_page = page_obj.number
        total_pages = paginator.num_pages
        page_range = []

        # Всегда добавляем первую страницу.
        page_range.append(1)

        # Добавляем диапазон вокруг текущей страницы.
        start = max(2, current_page - 2)
        end = min(total_pages - 1, current_page + 2)

        if start > 2:
            page_range.append('...')
        page_range.extend(range(start, end + 1))
        if end < total_pages - 1:
            page_range.append('...')

        # Всегда добавляем последнюю страницу (если больше 1).
        if total_pages > 1:
            page_range.append(total_pages)

        # Статистика по статусам (для фильтра).
        status_counts = {
            'all': UIP.objects.count(),
            'draft': UIP.objects.filter(status='draft').count(),
            'reserved_cz': UIP.objects.filter(status='reserved_cz').count(),
            'reserved_local': UIP.objects.filter(status='reserved_local').count(),
            'registered': UIP.objects.filter(status='registered').count(),
            'closed': UIP.objects.filter(status='closed').count(),
            'deleted': UIP.objects.filter(status='deleted').count(),
            'archived': UIP.objects.filter(status='archived').count(),
        }

        # Границы отображаемых записей (для текста "Показаны X-Y из Z").
        start_item = (page_obj.number - 1) * paginator.per_page + 1
        end_item = start_item + len(page_obj) - 1
        if paginator.count == 0:
            start_item = 0
            end_item = 0

        context.update({
            'title_name': 'Список УИП',
            'page_name': 'Список УИП',
            'page_obj': page_obj,
            'paginator': paginator,
            'page_range': page_range,
            'total_count': paginator.count,
            'current_status': status_filter,
            'status_counts': status_counts,
            'start_item': start_item,
            'end_item': end_item,
            'available_products': get_available_products(),
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

        if not product_sku_id or not production_date_str:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Не указаны обязательные параметры.'
                },
                status=400
            )

        try:
            production_date = datetime.strptime(production_date_str, '%Y-%m-%d').date()
        except ValueError:
            return JsonResponse(
                {
                    'is_error': True,
                    'message': 'Некорректный формат даты (ожидается ГГГГ-ММ-ДД).'
                },
                status=400
            )

        result = generate_uip(product_sku_id, production_date, mode)
        status_code = (
            200
            if not result.get('is_error')
            else 400
        )
        return JsonResponse(result, status=status_code)
