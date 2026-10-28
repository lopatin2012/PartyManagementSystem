# app_event/views.py

"""
Наблюдаемость: дашборд нагрузки и JSON-API метрик.

Страница `/monitoring/` и API `/monitoring/api/metrics/` доступны только
администраторам (`AdminRequiredMixin` / `admin_required_json`).
"""

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView

from app_helper import metrics
from app_helper.access import AdminRequiredMixin, admin_required_json


class MonitoringDashboardView(AdminRequiredMixin, TemplateView):
    """Страница «Мониторинг нагрузки» с графиками и топами."""

    template_name = 'monitoring/main.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['page_name'] = 'Мониторинг нагрузки'
        context['title_name'] = 'Мониторинг нагрузки'
        context['retention_days'] = metrics.retention_days()
        return context


@method_decorator(admin_required_json, name='dispatch')
class MonitoringMetricsView(View):
    """
    GET /monitoring/api/metrics/?range=1h|24h|7d

    Возвращает ряд точек, сводку за окно, топ-endpoint'ы и пороги.
    """

    def get(self, request):
        range_key = request.GET.get('range', metrics.DEFAULT_RANGE)
        data = metrics.get_dashboard_data(range_key)
        return JsonResponse({'is_error': False, **data})
