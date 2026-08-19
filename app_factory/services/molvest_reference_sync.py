# app_factory/services/molvest_reference_sync.py

import logging
from datetime import timedelta

import requests

from django.utils import timezone

from app_factory.models import (
    CardStateChoices,
    Factory,
    Line,
    PackagingLevelChoices,
    Product,
    ProductGroupChoices,
    ProductPackaging,
    ProductProductionLocation,
    ProductSKU,
    StateConditionChoices,
    Workshop,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

# Формат URL: {url}/workshop/api/v1/{endpoint}/
WORKSHOP_LIST_PATH = 'workshop/api/v1/workshop-list/'
LINE_LIST_PATH = 'workshop/api/v1/line-list/'
PRODUCT_LIST_PATH = 'workshop/api/v1/product-list/'
WORKSHOP_SET_UUID_PATH = 'workshop/api/v1/workshop-set-uuid/'
LINE_SET_UUID_PATH = 'workshop/api/v1/line-set-uuid/'
PRODUCT_SET_UUID_PATH = 'workshop/api/v1/product-set-uuid/'


# ==========================================
# HTTP-помощники.
# ==========================================

def _http_get(url: str, path: str):
    """GET с обработкой ошибок. Возвращает список или None."""
    try:
        response = requests.get(
            f'{url.rstrip("/")}/{path}',
            headers={'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f'Ошибка запроса {path}: {e}')
        return None

    if isinstance(data, dict) and data.get('is_error'):
        logger.error(f'Внешний сервис вернул ошибку ({path}): {data.get("message")}')
        return None
    if not isinstance(data, list):
        logger.error(f'Неожиданный формат ответа ({path}): {type(data).__name__}')
        return None
    return data


def _push_uuid(url: str, path: str, payload: dict) -> bool:
    """Отправляет uuid СУП во внешний сервис. Возвращает True при успехе."""
    try:
        response = requests.post(
            f'{url.rstrip("/")}/{path}',
            json=payload,
            headers={'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get('is_error'):
            logger.error(f'Внешний сервис не принял uuid ({path}): {data.get("message")}')
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f'Ошибка записи uuid ({path}): {e}')
        return False


# ==========================================
# Вспомогательные функции.
# ==========================================

def _normalize_gtin(value) -> str | None:
    """Приводит GTIN к 14 цифрам (EAN-13 дополняется ведущим нулём)."""
    raw = str(value or '').strip()
    if not raw.isdigit():
        return None
    if len(raw) == 14:
        return raw
    if len(raw) == 13:
        return f'0{raw}'
    if len(raw) == 8:
        return f'{"0" * 6}{raw}'
    return None


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ==========================================
# Синхронизация цехов.
# ==========================================

def _sync_workshops(url: str, factory: Factory, dry_run: bool) -> dict:
    summary = {'fetched': 0, 'created': 0, 'updated': 0, 'uuids_pushed': 0, 'errors': 0}

    data = _http_get(url, WORKSHOP_LIST_PATH)
    if data is None:
        summary['errors'] += 1
        return summary
    summary['fetched'] = len(data)

    for item in data:
        name = (item.get('name') or '').strip()
        if not name:
            continue
        # uuid_str в Molvest хранит uuid объекта СУП (совпадает с id СУП).
        sup_uuid = (item.get('uuid_str') or '').strip() or None

        existing = None
        if sup_uuid:
            existing = Workshop.objects.filter(id=sup_uuid).first()
        if existing is None:
            existing = Workshop.objects.filter(factory=factory, name=name).first()

        if existing:
            # Возвращаем наш uuid, если во внешнем сервисе его ещё нет.
            if sup_uuid != str(existing.id):
                if not dry_run and _push_uuid(url, WORKSHOP_SET_UUID_PATH, {
                    'name': name,
                    'uuid_str': sup_uuid,
                    'sup_uuid': str(existing.id),
                }):
                    summary['uuids_pushed'] += 1
            continue

        summary['created'] += 1
        if dry_run:
            continue
        new_workshop = Workshop.objects.create(factory=factory, name=name)
        if _push_uuid(url, WORKSHOP_SET_UUID_PATH, {
            'name': name,
            'uuid_str': sup_uuid,
            'sup_uuid': str(new_workshop.id),
        }):
            summary['uuids_pushed'] += 1

    return summary


# ==========================================
# Синхронизация линий.
# ==========================================

def _sync_lines(url: str, factory: Factory, dry_run: bool) -> dict:
    summary = {'fetched': 0, 'created': 0, 'updated': 0, 'uuids_pushed': 0, 'skipped': 0, 'errors': 0}

    data = _http_get(url, LINE_LIST_PATH)
    if data is None:
        summary['errors'] += 1
        return summary
    summary['fetched'] = len(data)

    workshops = {w.name: w for w in Workshop.objects.filter(factory=factory)}

    for item in data:
        name = (item.get('name') or '').strip()
        if not name:
            continue
        # uuid_str в Molvest хранит uuid объекта СУП (совпадает с id СУП).
        sup_uuid = (item.get('uuid_str') or '').strip() or None

        workshop_name = (item.get('workshop_name') or '').strip()
        workshop = workshops.get(workshop_name) if workshop_name else None
        if workshop is None:
            logger.warning(f'Линия «{name}»: не найден цех «{workshop_name}». Пропуск.')
            summary['skipped'] += 1
            continue

        existing = None
        if sup_uuid:
            existing = Line.objects.filter(id=sup_uuid).first()
        if existing is None:
            existing = Line.objects.filter(workshop=workshop, name=name).first()

        if existing:
            if sup_uuid != str(existing.id):
                if not dry_run and _push_uuid(url, LINE_SET_UUID_PATH, {
                    'name': name,
                    'uuid_str': sup_uuid,
                    'sup_uuid': str(existing.id),
                }):
                    summary['uuids_pushed'] += 1
            continue

        summary['created'] += 1
        if dry_run:
            continue
        new_line = Line.objects.create(workshop=workshop, name=name)
        if _push_uuid(url, LINE_SET_UUID_PATH, {
            'name': name,
            'uuid_str': sup_uuid,
            'sup_uuid': str(new_line.id),
        }):
            summary['uuids_pushed'] += 1

    return summary


# ==========================================
# Синхронизация продуктов.
# ==========================================

def _sync_products(url: str, factory: Factory, dry_run: bool) -> dict:
    summary = {
        'fetched': 0, 'created': 0, 'updated': 0,
        'skus_created': 0, 'packagings_created': 0, 'locations_created': 0,
        'uuids_pushed': 0, 'skipped': 0, 'errors': 0,
    }

    data = _http_get(url, PRODUCT_LIST_PATH)
    if data is None:
        summary['errors'] += 1
        return summary
    summary['fetched'] = len(data)

    lines = {
        **{str(l.id): l for l in Line.objects.filter(workshop__factory=factory)},
        **{l.name: l for l in Line.objects.filter(workshop__factory=factory)},
    }

    for item in data:
        code = (item.get('code') or '').strip()
        name = (item.get('name') or '').strip()
        gtin = _normalize_gtin(item.get('gtin'))
        if not code or not name:
            summary['skipped'] += 1
            continue
        # uuid_str в Molvest хранит uuid объекта СУП (совпадает с id СУП).
        sup_uuid = (item.get('uuid_str') or '').strip() or None
        active = bool(item.get('active', True))

        # === Находим/создаём Product ===
        existing = None
        if sup_uuid:
            existing = Product.objects.filter(id=sup_uuid).first()
        if existing is None:
            existing = Product.objects.filter(name=name).first()
        if existing is None and gtin:
            existing = Product.objects.filter(
                packagings__gtin=gtin,
                packagings__level=PackagingLevelChoices.UNIT,
            ).first()

        if existing:
            product = existing
            if not dry_run:
                _sync_product_details(
                    product, item, code, gtin, active, summary, lines,
                )
        else:
            summary['created'] += 1
            if dry_run:
                continue
            shelf_life = _as_int(item.get('date_expiration'), 0) or 0
            product = Product.objects.create(
                group=ProductGroupChoices.NOT_SELECTED,
                name=name,
                shelf_life_in_days=shelf_life,
                item_condition=StateConditionChoices.NOT_READY_ORDER_KM,
                card_status=CardStateChoices.DRAFT,
                is_active=active,
            )
            _sync_product_details(
                product, item, code, gtin, active, summary, lines,
            )

        # === Возвращаем наш uuid в Molvest ===
        if sup_uuid != str(product.id):
            if not dry_run and _push_uuid(url, PRODUCT_SET_UUID_PATH, {
                'code': code,
                'name': name,
                'uuid_str': sup_uuid,
                'sup_uuid': str(product.id),
            }):
                summary['uuids_pushed'] += 1

    return summary


def _sync_product_details(
        product: Product,
        item: dict,
        code: str,
        gtin: str | None,
        active: bool,
        summary: dict,
        lines: dict,
) -> None:
    """Создаёт/обновляет SKU, упаковку и места производства продукта."""
    # === SKU (артикул = код в учётной системе Molvest) ===
    sku = ProductSKU.objects.filter(article=code).first()
    if sku is None:
        sku = ProductSKU.objects.create(
            product=product,
            article=code,
            is_active=active,
        )
        summary['skus_created'] += 1
    elif sku.product_id != product.id:
        logger.warning(f'Артикул «{code}» уже занят другим продуктом. SKU не создан.')
        summary['skipped'] += 1
        sku = None

    if sku is None:
        return

    # === Упаковка (потребительская, уровень 1) ===
    if gtin:
        storage_days = _as_int(item.get('date_expiration_code'), 60) or 60
        code_tnved = (item.get('tn_ved') or '').strip() or None
        try:
            packaging, packaging_created = ProductPackaging.objects.update_or_create(
                product=product,
                level=PackagingLevelChoices.UNIT,
                defaults={
                    'gtin': gtin,
                    'quantity_inside': 1,
                    'code_storage_period_in_days': storage_days,
                    'code_tnved': code_tnved,
                    'is_active': active,
                },
            )
            if packaging_created:
                summary['packagings_created'] += 1
        except Exception as e:
            logger.warning(f'Упаковка {gtin}: не создана ({e}).')
            summary['skipped'] += 1

    # === Места производства (привязка к линиям) ===
    for line_ref in item.get('lines') or []:
        line_uuid = (line_ref.get('uuid_str') or '').strip() or None
        line_name = (line_ref.get('name') or '').strip()
        line = None
        if line_uuid:
            line = Line.objects.filter(id=line_uuid).first()
        if line is None and line_name:
            line = lines.get(line_name)
        if line is None:
            summary['skipped'] += 1
            continue
        ProductProductionLocation.objects.update_or_create(
            product_sku=sku,
            line=line,
            defaults={'is_active': True},
        )
        summary['locations_created'] += 1


# ==========================================
# Основная точка входа.
# ==========================================

def sync_molvest_reference(user=None, factory_ids: list = None, dry_run: bool = False) -> dict:
    """
    Синхронизация справочников цехов/линий/продуктов из Molvest.Маркировка в СУП.

    Для каждого действующего завода с заданным ip/port:
      1. Цеха — добавляем недостающие в СУП;
      2. Линии — по цехам, добавляем недостающие;
      3. Продукты — по линиям, раскладываем по Product / ProductSKU /
         ProductPackaging / ProductProductionLocation;
      4. После создания в СУП — возвращаем uuid объектов обратно в Molvest
         (заполняется uuid_str в Molvest).
    """
    summary = {
        'is_error': False,
        'factories': 0,
        'workshops': {},
        'lines': {},
        'products': {},
        'message': '',
    }

    factories = Factory.objects.filter(is_active=True)
    factories = factories.exclude(ip_address__isnull=True).exclude(port_address__isnull=True)
    if factory_ids:
        factories = factories.filter(id__in=factory_ids)

    if not factories.exists():
        summary['message'] = 'Нет действующих заводов с заданным ip-адресом/портом.'
        logger.warning(summary['message'])
        return summary

    for factory in factories:
        url = f'http://{factory.ip_address}:{factory.port_address}'
        logger.info(f'Синхронизация справочников завода «{factory.name}» ({url})')

        ws = _sync_workshops(url, factory, dry_run)
        ln = _sync_lines(url, factory, dry_run)
        pr = _sync_products(url, factory, dry_run)

        summary['factories'] += 1
        summary['workshops'][str(factory.id)] = ws
        summary['lines'][str(factory.id)] = ln
        summary['products'][str(factory.id)] = pr

        total_errors = ws['errors'] + ln['errors'] + pr['errors']
        if total_errors:
            summary['is_error'] = True

    mode = 'СУХОЙ ПРОГОН' if dry_run else 'Синхронизация'
    summary['message'] = (
        f'{mode} справочников завершена. Заводов: {summary["factories"]}. '
        f'Подробности по заводам в сводке.'
    )
    logger.info(summary['message'])
    return summary