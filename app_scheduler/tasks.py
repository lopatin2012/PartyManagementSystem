# app_scheduler/tasks.py

"""
Центральное хранилище всех фоновых задач.
Вызываются через .enqueue()
"""

import logging
from datetime import timedelta

from django.db.models import Exists, OuterRef
from django.tasks import task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Размер батча для bulk-операций
BATCH_SIZE = 5000


# ==========================================
# Честный Знак.
# ==========================================

@task(queue_name='default')
def refresh_suz_token_task() -> dict:
    """
    Обновление динамического токена СУЗ.
    :return:
    """

    from app_cz.models import SUZAccount
    from app_cz.services.suz_client import refresh_suz_dynamic_token

    # Проверка в необходимости обновления динамического токена.
    account = SUZAccount.objects.filter(is_active=True).first()
    if not account:
        message = 'СУЗ: нет активной учётной записи. Обновление токена пропущено.'
        logger.warning(message)
        return {
            'status': 'skipped',
            'is_error': True,
            'message': message
        }

    if account.dynamic_token and account.token_expires_at:
        # Обновление, если осталось меньше 2 часов.
        # Ранее порог был 30 минут — при интервале планировщика 6 часов
        # это создавало 4-часовой простой (токен живёт 8ч, на 6-м часу
        # он ещё "валиден" по порогу 30мин, а на 8-м уже истёк).
        if account.token_expires_at > timezone.now() + timedelta(hours=2):
            message = f"СУЗ: токен действителен до {account.token_expires_at:%Y-%m-%d %H:%M}. Обновление не требуется"
            logger.info(message)
            return {
                'status': 'skipped',
                'is_error': False,
                'message': message
            }

    success = refresh_suz_dynamic_token()

    if success:
        message = 'СУЗ: динамический токен успешно обновлён'
        logger.info(message)
        return {
            'status': 'ok',
            'is_error': False,
            'message': message
        }
    else:
        message = f'СУЗ: не удалось обновить динамический токен'
        logger.error(message)
        raise RuntimeError(message)


# ==========================================
# Управление жизненным циклом УИП.
# ==========================================

@task(queue_name='default')
def cleanup_expired_reserved_uips_task() -> dict:
    """
    УИП в статусе reserved_cz/reserved_local, которые не были зарегистрированы
    в течение 30 дней с момента резервирования → DELETED.

    Bulk-операции: 1 SELECT + 1 UPDATE + 1 INSERT.
    """
    from app_uip.models import UIP, UIPStatusLog, PartyStatusChoices

    threshold = timezone.now() - timedelta(days=30)

    # 1. Находим все подходящие УИП одним запросом.
    expired_uips = list(
        UIP.objects.filter(
            status__in=[
                PartyStatusChoices.RESERVED_CZ,
                PartyStatusChoices.RESERVED_LOCAL,
            ],
            reservation_date__lt=threshold.date(),
        ).values_list('id', 'status')
    )

    if not expired_uips:
        message = 'Нет УИП для удаления (30 дней без регистрации)'
        logger.info(message)
        return {'deleted': 0, 'message': message}

    # 2. Собираем ID и старые статусы для логов.
    uip_ids = [uip_id for uip_id, _ in expired_uips]
    old_statuses = {uip_id: status for uip_id, status in expired_uips}

    # 3. Обновление статусов.
    updated_count = UIP.objects.filter(id__in=uip_ids).update(
        status=PartyStatusChoices.DELETED,
        updated_at=timezone.now(),
    )

    # 4. Создание логов.
    log_entries = [
        UIPStatusLog(
            uip_id=uip_id,
            from_status=old_statuses[uip_id],
            to_status=PartyStatusChoices.DELETED,
            source='auto',
            note='Автоматическое удаление: 30 дней без регистрации (отсутствует отчёт о нанесении)',
        )
        for uip_id in uip_ids
    ]
    UIPStatusLog.objects.bulk_create(log_entries, batch_size=BATCH_SIZE)

    message = f'Удалено УИП без регистрации: {updated_count}'
    logger.info(message)
    return {'deleted': updated_count, 'message': message}


@task(queue_name='default')
def close_unused_registered_uips_task() -> dict:
    """
    УИП в статусе registered без производственных партий,
    которые не были использованы в течение 3 дней → CLOSED.
    """
    from app_uip.models import UIP, UIPStatusLog, PartyStatusChoices, ProductionParty

    threshold = timezone.now() - timedelta(days=3)

    # Подзапрос: нет связанных ProductionParty.
    no_parties = ~Exists(
        ProductionParty.objects.filter(uip_id=OuterRef('id'))
    )

    # Подзапрос: последний переход в REGISTERED был раньше порога.
    last_registered_before_threshold = Exists(
        UIPStatusLog.objects.filter(
            uip_id=OuterRef('id'),
            to_status=PartyStatusChoices.REGISTERED,
            created_at__lt=threshold,
        )
    )

    # 1. Находим все подходящие УИП одним запросом.
    unused_uips = list(
        UIP.objects.filter(
            status=PartyStatusChoices.REGISTERED,
        )
        .filter(no_parties)
        .filter(last_registered_before_threshold)
        .values_list('id', 'status')
    )

    if not unused_uips:
        message = 'Нет УИП для закрытия (3 дня без использования)'
        logger.info(message)
        return {'closed': 0, 'message': message}

    # 2. Собираем данные для логов.
    uip_ids = [uip_id for uip_id, _ in unused_uips]
    old_statuses = {uip_id: status for uip_id, status in unused_uips}

    # 3. Обновление статусов.
    updated_count = UIP.objects.filter(id__in=uip_ids).update(
        status=PartyStatusChoices.CLOSED,
        closed_at=timezone.now(),
        updated_at=timezone.now(),
    )

    # 4. Создание логов.
    log_entries = [
        UIPStatusLog(
            uip_id=uip_id,
            from_status=old_statuses[uip_id],
            to_status=PartyStatusChoices.CLOSED,
            source='auto',
            note='Автоматическое закрытие: 3 дня без прикрепления к производственной партии',
        )
        for uip_id in uip_ids
    ]
    UIPStatusLog.objects.bulk_create(log_entries, batch_size=BATCH_SIZE)

    message = f'Закрыто УИП без использования: {updated_count}'
    logger.info(message)
    return {'closed': updated_count, 'message': message}


@task(queue_name='default')
def archive_stale_closed_uips_task() -> dict:
    """
    УИП в статусе closed, которые не использовались в течение 30 дней → ARCHIVED.
    """
    from app_uip.models import UIP, UIPStatusLog, PartyStatusChoices

    threshold = timezone.now() - timedelta(days=30)

    # Подзапрос: последний переход в CLOSED был раньше порога.
    last_closed_before_threshold = Exists(
        UIPStatusLog.objects.filter(
            uip_id=OuterRef('id'),
            to_status=PartyStatusChoices.CLOSED,
            created_at__lt=threshold,
        )
    )

    # 1. Находим все подходящие УИП одним запросом.
    stale_uips = list(
        UIP.objects.filter(
            status=PartyStatusChoices.CLOSED,
        )
        .filter(last_closed_before_threshold)
        .values_list('id', 'status')
    )

    if not stale_uips:
        message = 'Нет УИП для архивации (30 дней после закрытия)'
        logger.info(message)
        return {'archived': 0, 'message': message}

    # 2. Собираем данные для логов.
    uip_ids = [uip_id for uip_id, _ in stale_uips]
    old_statuses = {uip_id: status for uip_id, status in stale_uips}

    # 3. Bulk-update статусов.
    updated_count = UIP.objects.filter(id__in=uip_ids).update(
        status=PartyStatusChoices.ARCHIVED,
        archived_at=timezone.now(),
        updated_at=timezone.now(),
    )

    # 4. Bulk-create логов.
    log_entries = [
        UIPStatusLog(
            uip_id=uip_id,
            from_status=old_statuses[uip_id],
            to_status=PartyStatusChoices.ARCHIVED,
            source='auto',
            note='Автоматическая архивация: 30 дней без активности после закрытия',
        )
        for uip_id in uip_ids
    ]
    UIPStatusLog.objects.bulk_create(log_entries, batch_size=BATCH_SIZE)

    message = f'Архивировано закрытых УИП: {updated_count}'
    logger.info(message)
    return {'archived': updated_count, 'message': message}

# ==========================================
# Очистка старых данных.
# ==========================================

@task(queue_name='default')
def cleanup_old_logs_task() -> dict:
    """Очистка логов статусов УИП старше 700 дней."""
    from app_uip.models import UIPStatusLog

    threshold = timezone.now() - timedelta(days=700)
    deleted_count, _ = UIPStatusLog.objects.filter(
        created_at__lt=threshold
    ).delete()

    logger.info(f'Удалено старых логов: {deleted_count}')
    return {'deleted': deleted_count}


# ==========================================
# Синхронизация с внешним сервисом (Молвест.Маркировка).
# ==========================================

@task(queue_name='default')
def sync_external_parties_codes_task() -> dict:
    """
    Периодическая синхронизация производственных партий и их кодов (раз в час).

    - Выгружает из внешнего сервиса задания, изменённые после последней
      успешной синхронизации, и обновляет ProductionParty
      (статусы, количества, УИП, линия).
    - Синхронизирует коды маркировки по всем внешним заданиям.

    Адрес сервера маркировки берётся из модели Factory (ip_address/port_address).
    """
    from app_cz.services.code_sync import sync_external_parties_and_codes

    return sync_external_parties_and_codes(
        task_path=f'{__name__}.sync_external_parties_codes_task'
    )


# ==========================================
# Мониторинг резерва УИП.
# ==========================================

@task(queue_name='default')
def check_uip_reserve_task() -> dict:
    """
    Периодическая проверка заполнения резерва УИП и уведомления по почте.

    - >50% — предупреждение, >80% — тревога.
    - >90% — снятие с резерва устаревших УИП через отчёт о нанесении
      (кодами DataMatrix из заданий, либо кодом по GTIN из внешнего сервиса).
    """
    from app_cz.services.reserve_monitor import check_uip_reserve_and_notify

    return check_uip_reserve_and_notify()


# ==========================================
# Национальный каталог.
# ==========================================

@task(queue_name='default')
def sync_national_catalog_task() -> dict:
    """
    Периодическая синхронизация товаров Национального каталога (раз в час).

    Выгружает товары по ИНН активной учётной записи СУЗ (owner_inn по умолчанию)
    и обновляет локальные Product/ProductPackaging из данных НК.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    actor = User.objects.filter(is_superuser=True).first()
    if not actor:
        message = (
            'НК: нет пользователя-администратора для авторизации True API. '
            'Синхронизация пропущена.'
        )
        logger.warning(message)
        return {'status': 'skipped', 'is_error': True, 'message': message}

    from app_cz.services.national_catalog_client import sync_products
    from app_factory.services.nk_sync_service import sync_nk_to_products

    nk_result = sync_products(actor)
    product_result = sync_nk_to_products(user=actor)

    message = (
        f'НК: синхронизировано {nk_result.get("total", 0)} товаров '
        f'(создано {nk_result.get("created", 0)}, обновлено {nk_result.get("updated", 0)}, '
        f'ошибок {nk_result.get("errors", 0)}); '
        f'продукты: создано {product_result.get("products_created", 0)}, '
        f'обновлено {product_result.get("products_updated", 0)}, '
        f'упаковок создано {product_result.get("packagings_created", 0)}, '
        f'SKU создано {product_result.get("skus_created", 0)}'
    )
    logger.info(message)
    return {
        'status': 'ok',
        'is_error': False,
        'message': message,
        'nk': nk_result,
        'products': product_result,
    }


# ==========================================
# Электронная почта (очередь 'emails').
# ==========================================

@task(queue_name='emails')
def send_email_task(
        subject: str,
        message: str,
        recipient_list: list,
        html_message: str = None,
) -> dict:
    """
    Отправка электронного письма в фоне (очередь 'emails').

    :param subject: Тема письма.
    :param message: Текст письма.
    :param recipient_list: Список получателей.
    :param html_message: HTML-версия письма (опционально).
    :return: Словарь с результатом.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    recipients = [r for r in (recipient_list or []) if r]
    if not recipients:
        warning = 'Не указаны получатели письма.'
        logger.warning(warning)
        return {'sent': False, 'message': warning}

    try:
        sent = send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f'Email отправлен: «{subject}» -> {recipients}')
        return {'sent': True, 'count': sent, 'recipients': recipients}
    except Exception as e:
        logger.error(f'Ошибка отправки email «{subject}»: {e}', exc_info=True)
        raise