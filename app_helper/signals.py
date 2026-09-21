# app_helper/signals.py

"""
Автоматическое создание групп доступа после миграций.

Группы:
- «Админ» — все права;
- «Просмотр» — только просмотр УИП (app_uip.view_uip).

Сигнал post_migrate срабатывает при каждом `manage.py migrate` (в т.ч. в CI и
deploy-скриптах), поэтому группы гарантированно существуют на любой БД.
Операция идемпотентна: существующие группы и права переиспользуются.
"""

import logging

from django.contrib.auth.models import Group, Permission
from django.db.models.signals import post_migrate
from django.dispatch import receiver

from app_helper.access import ROLE_ADMIN, ROLE_VIEW, UIP_VIEW_PERM

logger = logging.getLogger(__name__)


@receiver(post_migrate, dispatch_uid='app_helper_setup_default_groups')
def setup_default_groups(sender, app_config=None, **kwargs):
    """Создаёт/обновляет роли «Админ» и «Просмотр» (идемпотентно)."""
    if app_config is None or app_config.name != 'app_helper':
        return

    try:
        admin_group, _ = Group.objects.get_or_create(name=ROLE_ADMIN)
        admin_group.permissions.set(Permission.objects.all())

        view_group, _ = Group.objects.get_or_create(name=ROLE_VIEW)
        view_perm = Permission.objects.filter(
            content_type__app_label='app_uip',
            codename='view_uip',
        )
        view_group.permissions.set(view_perm)

        logger.info(
            'Группы доступа настроены: «%s» (все права), «%s» (%s).',
            ROLE_ADMIN, ROLE_VIEW, UIP_VIEW_PERM,
        )
    except Exception:
        # Не роняем migrate из-за прав (например, таблиц прав ещё нет).
        logger.warning('Не удалось настроить группы доступа', exc_info=True)
