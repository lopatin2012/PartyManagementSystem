from django.apps import AppConfig


class AppHelperConfig(AppConfig):
    name = 'app_helper'
    verbose_name = 'Модуль-помощник'

    def ready(self):
        # Регистрируем создание групп доступа после миграций.
        from app_helper import signals  # noqa: F401
