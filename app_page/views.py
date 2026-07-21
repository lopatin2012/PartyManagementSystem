# app_page/views.py

import logging

from django.views.generic import TemplateView
from django.shortcuts import render
from django.views import View
from django.core.paginator import Paginator
from django.db.models import Q

from app_cz.models import CISCode
from app_uip.models import UIP, ProductionParty

from app_helper.search_helper import detect_search_type

from config.settings import (
    DEBUG, SERVICE_MODE_TEXT, SERVICE_MODE_COLOR, SERVICE_VERSION
)

logger = logging.getLogger(__name__)

class MainPageView(TemplateView):
    """Главная страница."""
    template_name = 'main/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Определяем имя пользователя.
        if self.request.user.is_authenticated:
            user_name = (
                    self.request.user.get_full_name()
                    or self.request.user.username
            )
        else:
            user_name = 'Неизвестный'

        context.update(
            {
                'title_name': 'Система управления партиями',
                'page_name': 'Главная страница',
                'user_name': user_name,
                'is_authenticated': self.request.user.is_authenticated,
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
