# app_cz/services/code_sync.py

"""
Синхронизация данных между сервисами.

Содержит:
- приёмник производственной партии (задания) из внешнего сервиса (Молвест.Маркировка);
- синхронизацию кодов маркировки (CISCode) по заданию из внешнего сервиса;
- периодический синхронизатор (sync_all_external_tasks), вызываемый планировщиком.

Схема обмена:
1. Внешний сервис генерирует задание и запрашивает у нас УИП (api/v1/generate-uip/),
   получая в ответе number (номер УИП), uuid_uip (id УИП) и operation_id (uuid операции).
2. Внешний сервис периодически «пушит» задание в наш приёмник (api/tasks/receive/).
3. Мы периодически забираем коды из внешнего сервиса по заданию (sync_codes_task).
4. Активные статусы задания («Создано», «В работе», «Закрыто») — синхронизируем коды.
   Финальный статус «Завершено» — последняя синхронизация данных из задания.
"""

import logging
from datetime import datetime, time

import requests
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from app_cz.models import (
    CISCode,
    CISCodesStatusChoices,
    ProductionCodeStatusChoices,
)
from app_uip.models import (
    UIP,
    ProductionParty,
    ProductionPartyStatusChoices,
    ProductionPartySyncStatusChoices,
)
from app_factory.models import Line, ProductPackaging, PackagingLevelChoices

logger = logging.getLogger(__name__)

# Номер партии по умолчанию во внешнем сервисе (означает «УИП не присвоен»).
NUMBER_PARTY_REGISTRATION = '00000000000000000000000000000000'


# ==========================================
# Маппинг статусов внешнего сервиса.
# ==========================================

EXTERNAL_STATUS_MAP = {
    'Проверка': ProductionPartyStatusChoices.CHECK,
    'Создано': ProductionPartyStatusChoices.CREATED,
    'В работе': ProductionPartyStatusChoices.WORK,
    'Закрыто': ProductionPartyStatusChoices.CLOSED,
    'Завершено': ProductionPartyStatusChoices.COMPLETED,
    'Удалено': ProductionPartyStatusChoices.DELETED,
    'Архив': ProductionPartyStatusChoices.ARCHIVED,
    'Ошибка': ProductionPartyStatusChoices.ERROR,
}

# Активные статусы задания — синхронизируем коды постоянно.
ACTIVE_SYNC_STATUSES = [
    ProductionPartyStatusChoices.CREATED,
    ProductionPartyStatusChoices.WORK,
    ProductionPartyStatusChoices.CLOSED,
]

# Финальный статус задания — последняя синхронизация данных из него.
FINAL_SYNC_STATUS = ProductionPartyStatusChoices.COMPLETED

# Статусы, при которых синхронизация кодов не выполняется.
NO_SYNC_STATUSES = [
    ProductionPartyStatusChoices.CHECK,
    ProductionPartyStatusChoices.DELETED,
    ProductionPartyStatusChoices.ARCHIVED,
    ProductionPartyStatusChoices.ERROR,
]


def _map_external_status(raw: str):
    """
    Преобразует статус задания из внешнего сервиса (русское название)
    во внутренний код ProductionPartyStatusChoices.
    """
    if not raw:
        return ProductionPartyStatusChoices.CREATED
    # Пробуем как код, потом как русское название.
    if raw in ProductionPartyStatusChoices.values:
        return ProductionPartyStatusChoices(raw).value
    return EXTERNAL_STATUS_MAP.get(
        raw.strip(), ProductionPartyStatusChoices.ERROR
    )


def _parse_int(value) -> int:
    """Безопасный парсинг целого (значения приходят строками/Decimal)."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return 0


def _parse_dt(value):
    """ISO datetime → timezone-aware datetime (или None)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = parse_datetime(str(value).replace('Z', '+00:00'))
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _parse_date_as_dt(value):
    """ISO date → datetime (начало дня, timezone-aware) или None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    d = parse_date(str(value))
    if not d:
        return None
    return timezone.make_aware(datetime.combine(d, time.min))


# ==========================================
# Вспомогательные функции.
# ==========================================

def _mark_party_sync(party: ProductionParty, ok: bool, message: str):
    """Фиксирует результат синхронизации на производственной партии."""
    party.sync_status = (
        ProductionPartySyncStatusChoices.SYNCED
        if ok
        else ProductionPartySyncStatusChoices.ERROR
    )
    party.last_sync_at = timezone.now()
    party.last_sync_message = (message or '')[:2000]
    party.save(update_fields=[
        'sync_status', 'last_sync_at', 'last_sync_message', 'updated_at'
    ])


def _recalculate_uip_quantities(uip: UIP):
    """Пересчитывает плановое/фактическое количество УИП по партиям."""
    if not uip:
        return
    from django.db.models import Sum
    agg = uip.production_parties.aggregate(
        planned=Sum('planned_quantity'),
        produced=Sum('produced_quantity'),
    )
    planned = agg['planned'] or 0
    produced = agg['produced'] or 0
    if planned != uip.planned_quantity or produced != uip.produced_quantity:
        uip.planned_quantity = planned
        uip.produced_quantity = produced
        uip.save(update_fields=['planned_quantity', 'produced_quantity', 'updated_at'])


def find_uip_for_external_task(data: dict):
    """
    Находит УИП по данным задания внешнего сервиса.

    Приоритет:
    1. По номеру УИП (поле `uip`), если он присвоен (не заглушка).
    2. По UUID операции генерации (operation_id/uuid_str), если внешний сервис
       сохранил его в задании.
    """
    number = str(data.get('uip') or '').strip()
    if number and number != NUMBER_PARTY_REGISTRATION:
        uip = UIP.objects.filter(number=number).first()
        if uip:
            return uip

    operation_id = str(
        data.get('operation_id') or data.get('uuid_str') or ''
    ).strip()
    if operation_id:
        try:
            uip = UIP.objects.filter(operation_uuid=operation_id).first()
        except (ValueError, TypeError):
            uip = None
        if uip:
            return uip

    return None


def _find_line(data: dict):
    """
    Пытается сопоставить линию внешнего сервиса с локальной линией (по имени).
    """
    line_ref = data.get('line')
    if not line_ref:
        return None
    if isinstance(line_ref, dict):
        line_ref = line_ref.get('name') or line_ref.get('id')
    if not line_ref:
        return None
    return Line.objects.filter(name=str(line_ref)).first()


def get_factory_for_party(party: ProductionParty):
    """
    Завод, на котором выполняется задание (через линию → цех → завод).

    Адрес локального сервера маркировки хранится именно в модели Factory
    (ip_address + port_address), т.к. у каждого завода свой сервер.
    """
    if not party.line or not party.line.workshop:
        return None
    return party.line.workshop.factory


def build_external_service_url(party: ProductionParty) -> str:
    """
    Формирует базовый URL локального сервера маркировки завода
    из модели Factory (ip_address + port_address).
    """
    factory = get_factory_for_party(party)
    if not factory:
        return None
    if not factory.ip_address or not factory.port_address:
        return None
    return f'http://{factory.ip_address}:{factory.port_address}'


# ==========================================
# Приёмник производственной партии (задания).
# ==========================================

def receive_external_task(data: dict) -> dict:
    """
    Приёмник производственной партии (задания) из внешнего сервиса.

    Создаёт или обновляет ProductionParty по идентификатору задания
    (uuid_str / task_uuid / task_id / id). Статусы заданий внешнего сервиса
    преобразуются во внутренние.

    :param data: Словарь с данными задания (поля модели Task внешнего сервиса).
    :return: Словарь с результатом.
    """
    external_id = str(
        data.get('uuid_str')
        or data.get('task_uuid')
        or data.get('task_id')
        or data.get('id')
        or ''
    ).strip()

    if not external_id:
        return {
            'has_error': True,
            'message': 'Не указан идентификатор задания (uuid_str/task_id/id).'
        }

    # === Статус ===
    status = _map_external_status(data.get('status'))

    # === УИП ===
    uip = find_uip_for_external_task(data)

    # === Поиск существующей партии по внешнему id ===
    party = ProductionParty.objects.filter(external_number_task=external_id).first()
    is_new = party is None

    if party is None:
        party = ProductionParty(
            external_number_task=external_id,
            uip=uip,
            is_external=True,
        )

    try:
        with transaction.atomic():
            # Привязка УИП (если задание его получило).
            if uip:
                party.uip = uip

            party.production_party = str(data.get('party') or '')[:32] or party.production_party
            party.status = status
            party.is_external = True

            # Линия.
            line = _find_line(data)
            if line:
                party.line = line

            # Даты.
            start_work = _parse_dt(data.get('start_work'))
            end_work = _parse_dt(data.get('end_work'))
            date_marking = _parse_date_as_dt(data.get('date_marking'))
            date_expiration = _parse_date_as_dt(data.get('date_expiration'))
            if start_work:
                party.production_datetime_start = start_work
            if end_work:
                party.production_datetime_end = end_work
            if date_marking:
                party.marking_datetime = date_marking
            if date_expiration:
                party.expiration_datetime = date_expiration

            # Количества.
            plan_amount = _parse_int(data.get('plan_amount'))
            amount = _parse_int(data.get('amount'))
            if plan_amount:
                party.planned_quantity = plan_amount
            if amount:
                party.produced_quantity = amount

            # Дата производства задания → дата производства УИП (если пустая).
            date_work = parse_date(str(data.get('date_work'))) if data.get('date_work') else None
            if (
                    date_work
                    and uip
                    and not uip.production_date
            ):
                uip.production_date = date_work
                uip.save(update_fields=['production_date', 'updated_at'])

            # Статус синхронизации: активное/финальное задание снова ожидает синхронизации.
            if status in ACTIVE_SYNC_STATUSES or status == FINAL_SYNC_STATUS:
                party.sync_status = ProductionPartySyncStatusChoices.PENDING

            party.save()

            if uip:
                _recalculate_uip_quantities(uip)
    except Exception as e:
        logger.exception(f'Ошибка при приёме задания {external_id}: {e}')
        return {
            'has_error': True,
            'message': f'Ошибка сохранения задания: {str(e)}'
        }

    message = (
        f'Задание {external_id} создано (статус: {party.get_status_display()})'
        if is_new
        else f'Задание {external_id} обновлено (статус: {party.get_status_display()})'
    )
    logger.info(message)

    return {
        'has_error': False,
        'created': is_new,
        'party_id': str(party.id),
        'uip_id': str(uip.id) if uip else None,
        'status': party.status,
        'message': message,
    }


# ==========================================
# Маппинг статусов кодов.
# ==========================================

def _map_production_status(working_code: dict) -> int:
    """
    Преобразует статусы в production_status нового проекта.

    Логика:
    - Если status_application = True → APPLIED (нанесён)
    - Иначе → FREE (свободный)
    """
    if working_code.get('status_application'):
        return ProductionCodeStatusChoices.APPLIED
    return ProductionCodeStatusChoices.FREE


def _map_cz_status(code: dict) -> int:
    """
    Преобразует булевы флаги статуса из рабочего проекта в IntegerChoices нового.

    Логика приоритета (от высшего к низшему):
    1. status_introduction_into_circulation → INTRODUCED_INTO_CIRCULATION
    2. status_application → APPLIED
    3. status_emission_lived → EMITTED
    4. status_delete / laboratory → WITHDRAWN_FROM_CIRCULATION
    5. По умолчанию → EMITTED
    """
    if code.get('status_introduction_into_circulation'):
        return CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION
    if code.get('status_application'):
        return CISCodesStatusChoices.APPLIED
    if code.get('status_delete') or code.get('laboratory'):
        return CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION
    return CISCodesStatusChoices.EMITTED


# ==========================================
# Синхронизация кодов маркировки.
# ==========================================

def _packaging_for_level(product, default_packaging, level: int):
    """Упаковка продукта для уровня; default — если уровень совпадает."""
    if level == default_packaging.level:
        return default_packaging
    if not product:
        return None
    return product.packagings.filter(level=level, is_active=True).first()


def _upsert_codes(party: ProductionParty, default_packaging: ProductPackaging, codes: list) -> dict:
    """
    Создаёт недостающие коды и обновляет статусы существующих.

    :return: {'created': int, 'updated': int, 'skipped': int}
    """
    product = None
    if party.uip and party.uip.product_sku:
        product = party.uip.product_sku.product

    created_count = 0
    updated_count = 0
    skipped_count = 0

    if not codes:
        return {'created': 0, 'updated': 0, 'skipped': 0}

    incoming = [
        c for c in codes
        if isinstance(c, dict) and c.get('code')
    ]
    if not incoming:
        return {'created': 0, 'updated': 0, 'skipped': len(codes)}

    code_strings = [c['code'] for c in incoming]
    existing_map = {
        obj.code: obj
        for obj in CISCode.objects.filter(code__in=code_strings)
    }

    new_codes = []
    updates = []
    parent_refs = {}  # code -> parent_code

    for code_dict in incoming:
        code_str = code_dict['code']

        # Уровень: из данных кода (если валиден) или из упаковки по умолчанию.
        raw_level = code_dict.get('level')
        try:
            level = int(raw_level) if raw_level is not None else default_packaging.level
            if level not in PackagingLevelChoices.values:
                level = default_packaging.level
        except (ValueError, TypeError):
            level = default_packaging.level

        packaging = _packaging_for_level(product, default_packaging, level)
        if packaging is None:
            skipped_count += 1
            continue

        cz_status = _map_cz_status(code_dict)
        production_status = _map_production_status(code_dict)

        parent_code = code_dict.get('parent_code') or code_dict.get('parent')
        if parent_code:
            parent_refs[code_str] = str(parent_code)

        existing = existing_map.get(code_str)
        if existing:
            # Обновляем статусы, если изменились.
            if (
                    existing.cz_status != cz_status
                    or existing.production_status != production_status
            ):
                existing.cz_status = cz_status
                existing.production_status = production_status
                updates.append(existing)
                updated_count += 1
            else:
                skipped_count += 1
            continue

        new_codes.append(CISCode(
            production_party=party,
            product_packaging=packaging,
            code=code_str,
            level=packaging.level,
            cz_status=cz_status,
            production_status=production_status,
            parent=None,
        ))

    # 1. Создание новых кодов.
    if new_codes:
        with transaction.atomic():
            created = CISCode.objects.bulk_create(
                new_codes,
                batch_size=5000,
                ignore_conflicts=True,
            )
        created_count = len(created)

    # 2. Обновление статусов существующих.
    if updates:
        with transaction.atomic():
            CISCode.objects.bulk_update(
                updates,
                fields=['cz_status', 'production_status', 'updated_at'],
                batch_size=5000,
            )

    # 3. Связи parent/children (агрегация) — после создания всех кодов.
    if parent_refs:
        parent_updates = []
        for code_str, parent_code in parent_refs.items():
            child = existing_map.get(code_str)
            if child is None:
                child = CISCode.objects.filter(code=code_str).first()
            if not child or child.parent_id:
                continue
            parent = CISCode.objects.filter(code=parent_code).first()
            if parent and parent.id != child.id:
                child.parent = parent
                parent_updates.append(child)
        if parent_updates:
            with transaction.atomic():
                CISCode.objects.bulk_update(
                    parent_updates,
                    fields=['parent', 'updated_at'],
                    batch_size=5000,
                )

    return {'created': created_count, 'updated': updated_count, 'skipped': skipped_count}


def sync_codes_task(
        url: str,
        task_id: str,
        production_party_id: str,
        token: str = None,
        packaging_id: str = None,
        level: int = None,
) -> dict:
    """
    Получает коды задания из внешнего сервиса и синхронизирует их локально.

    :param url: Базовый URL API внешнего сервиса (например, http://192.168.1.100:8000).
    :param task_id: ID задания (Task) во внешнем сервисе.
    :param production_party_id: ID ProductionParty в этом проекте.
    :param token: Токен авторизации для API внешнего сервиса (если требуется).
    :param packaging_id: ID ProductPackaging (необязательно; по умолчанию
                         берётся потребительская упаковка продукта партии).
    :param level: Уровень упаковки (необязательно; используется уровень упаковки).
    :return: Словарь с результатом синхронизации.
    """
    # 1. Проверка существования записей.
    try:
        party = ProductionParty.objects.select_related(
            'uip__product_sku__product'
        ).get(id=production_party_id)
    except (ProductionParty.DoesNotExist, ValueError, TypeError):
        return {
            'has_error': True,
            'message': f'Производственная партия не найдена: {production_party_id}'
        }

    if not task_id:
        _mark_party_sync(party, False, 'У задания отсутствует идентификатор во внешнем сервисе.')
        return {
            'has_error': True,
            'message': 'У задания отсутствует идентификатор во внешнем сервисе.'
        }

    # 2. Определяем упаковку (GTIN), к которой относятся коды.
    packaging = None
    if packaging_id:
        packaging = ProductPackaging.objects.filter(id=packaging_id).first()

    if packaging is None:
        product = (
            party.uip.product_sku.product
            if party.uip and party.uip.product_sku
            else None
        )
        if product is None:
            _mark_party_sync(
                party, False,
                'Не удалось определить продукт партии (нет привязанного УИП).'
            )
            return {
                'has_error': True,
                'message': 'Не удалось определить продукт партии (нет привязанного УИП).'
            }
        packaging = product.packagings.filter(
            level=PackagingLevelChoices.UNIT,
            is_active=True,
        ).first()
        if packaging is None:
            _mark_party_sync(
                party, False,
                f'Для продукта «{product.name}» не найдена потребительская упаковка (GTIN).'
            )
            return {
                'has_error': True,
                'message': (
                    f'Для продукта «{product.name}» не найдена '
                    f'потребительская упаковка (GTIN).'
                )
            }

    # 3. Запрашиваем коды из внешнего сервиса.
    api_url = f"{url.rstrip('/')}/codes/api/get_codes_by_task/"
    params = {'task_id': str(task_id)}
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Token {token}'

    try:
        logger.info(f'Запрос кодов из внешнего сервиса: task_id={task_id}')
        response = requests.get(api_url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f'Ошибка запроса к внешнему сервису: {e}')
        _mark_party_sync(party, False, f'Не удалось получить коды: {str(e)}')
        return {
            'has_error': True,
            'message': f'Не удалось получить коды из внешнего сервиса: {str(e)}'
        }

    if isinstance(payload, dict):
        codes = (
            payload.get('results')
            or payload.get('data')
            or payload.get('codes')
            or []
        )
    else:
        codes = payload or []

    if not codes:
        message = 'Внешний сервис вернул пустой список кодов.'
        _mark_party_sync(party, True, message)
        logger.info(f'{message} task_id={task_id}')
        return {
            'has_error': False,
            'message': message,
            'synced_count': 0,
            'updated_count': 0,
            'skipped_count': 0,
        }

    # 4. Создаём/обновляем коды.
    logger.info(f'Получено {len(codes)} кодов из внешнего сервиса (task_id={task_id})')
    stats = _upsert_codes(party, packaging, codes)

    message = (
        f'Синхронизировано кодов: создано {stats["created"]}, '
        f'обновлено {stats["updated"]}, пропущено {stats["skipped"]}'
    )
    _mark_party_sync(party, True, message)

    return {
        'has_error': False,
        'message': message,
        'synced_count': stats['created'],
        'updated_count': stats['updated'],
        'skipped_count': stats['skipped'],
    }


def sync_codes_for_party(party: ProductionParty, url: str = None, token: str = None) -> dict:
    """
    Синхронизирует коды для одной производственной партии.

    Адрес внешнего сервиса берётся из завода партии (Factory.ip_address/port_address),
    если не передан явно через url. Каждый завод имеет собственный сервер маркировки.
    """
    factory = get_factory_for_party(party)

    if not url:
        url = build_external_service_url(party)
        if not url:
            if not factory:
                message = (
                    'Не определён завод для партии (нет привязки линии → цех → завод). '
                    'Синхронизация кодов невозможна.'
                )
            else:
                message = (
                    f'У завода «{factory.name}» не задан ip-адрес/порт '
                    f'(Factory.ip_address / Factory.port_address).'
                )
            _mark_party_sync(party, False, message)
            return {'has_error': True, 'message': message}

    if factory:
        logger.info(
            f'Синхронизация кодов партии {party.external_number_task} '
            f'через сервер завода «{factory.name}» ({url})'
        )

    return sync_codes_task(
        url=url,
        task_id=party.external_number_task,
        production_party_id=str(party.id),
        token=token or None,
    )


# ==========================================
# Периодический синхронизатор.
# ==========================================

def sync_all_external_tasks() -> dict:
    """
    Периодическая синхронизация данных с внешними сервисами заводов.

    Задания приходят во внешний сервис через приёмник (api/tasks/receive/).
    Здесь выполняется синхронизация кодов маркировки:
    - для заданий в активных статусах («Создано», «В работе», «Закрыто»);
    - для заданий в финальном статусе («Завершено») — последняя синхронизация.

    Адрес сервера маркировки определяется по заводу партии (Factory.ip_address/port_address).

    :return: Сводка по синхронизации.
    """
    summary = {
        'is_error': False,
        'parties_synced': 0,
        'codes_created': 0,
        'codes_updated': 0,
        'errors': 0,
        'message': '',
    }

    # Синхронизация кодов для всех внешних заданий.
    parties = ProductionParty.objects.filter(
        is_external=True,
    ).select_related(
        'uip__product_sku__product',
        'line__workshop__factory',
    )

    for party in parties:
        # Активные статусы — синхронизируем каждый цикл.
        if party.status in ACTIVE_SYNC_STATUSES:
            result = sync_codes_for_party(party)
        # Финальный статус — последняя синхронизация (до первого успеха).
        elif (
                party.status == FINAL_SYNC_STATUS
                and party.sync_status != ProductionPartySyncStatusChoices.SYNCED
        ):
            result = sync_codes_for_party(party)
        else:
            continue

        summary['parties_synced'] += 1
        if result.get('has_error'):
            summary['errors'] += 1
        else:
            summary['codes_created'] += result.get('synced_count', 0)
            summary['codes_updated'] += result.get('updated_count', 0)

    parts = [
        f'партий синхронизировано: {summary["parties_synced"]}',
        f'кодов создано: {summary["codes_created"]}',
        f'кодов обновлено: {summary["codes_updated"]}',
    ]
    if summary['errors']:
        parts.append(f'⚠ ошибок: {summary["errors"]}')
    summary['message'] = 'Синхронизация завершена. ' + ', '.join(parts)
    summary['is_error'] = summary['errors'] > 0

    logger.info(summary['message'])
    return summary
