# app_page/views.py

import logging

from django.views.generic import TemplateView

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

        # Имитация счётчика.
        requests_count = 1000

        context.update(
            {
                'title_name': 'Система управления партиями',
                'page_name': 'Главная страница',
                'user_name': user_name,
                'is_authenticated': self.request.user.is_authenticated,
                'service_version': SERVICE_VERSION,
                'requests_count': requests_count,
                'service_mode_name': SERVICE_MODE_TEXT,
                'service_mode_color': SERVICE_MODE_COLOR
            }
        )

        return context
