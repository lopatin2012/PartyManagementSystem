# app_factory/services/nk_sync_service.py

"""
Автоматическая синхронизация Product и ProductPackaging из данных
Национального каталога (NationalCatalogProduct).

Вызывается после sync_products() для обновления локальных справочников
продукции на основе данных из НК.
"""

import logging
from typing import Dict, List, Optional

from app_factory.models import (
    CardStateChoices,
    NationalCatalogProduct,
    PackagingLevelChoices,
    Product,
    ProductPackaging,
    ProductSKU,
    StateConditionChoices,
)

logger = logging.getLogger(__name__)

# Имя атрибута в карточке НК (good_attrs), содержащего код товара в учётной
# системе поставщика (источник для «Кода внутри организации» / SKU.article).
SUPPLIER_CODE_ATTR = 'Код товара в учетной системе поставщика'


def _extract_supplier_codes(raw_data: Dict) -> List[str]:
    """Все коды товара в учётной системе поставщика из атрибутов НК.

    ВАЖНО: атрибуты карточки приходят в поле good_attrs (не attrs) —
    это подтверждено реальными данными НК (good_attrs с attr_name=
    «Код товара в учетной системе поставщика»). Атрибут может встречаться
    в карточке несколько раз; значения приходят как один атрибут с
    запятыми («60379, 65942, 60378, 60397») — разворачиваем в список.
    """
    codes = []
    for attr in raw_data.get('good_attrs') or raw_data.get('attrs') or []:
        if attr.get('attr_name') != SUPPLIER_CODE_ATTR:
            continue
        value = str(attr.get('attr_value', '') or '').strip()
        for part in value.replace(';', ',').split(','):
            part = part.strip()
            if part and part not in codes:
                codes.append(part)
    return codes


def _sync_product_sku(product: Product, nk: NationalCatalogProduct) -> bool:
    """Создаёт/обновляет ProductSKU из кодов учётной системы поставщика.

    - Первый код → article («Код внутри организации»).
    - Остальные/все коды → other_codes («Другие коды внутри организации»).
    - SKU создаётся только если у продукта ещё нет ни одной номенклатуры.
    - При синхронизации article не обновляется (только other_codes).

    Возвращает True, если SKU был создан.
    """
    codes = _extract_supplier_codes(nk.raw_data or {})
    if not codes:
        return False

    # Нет поля created_at у ProductSKU — берём первый по id (порядок создания).
    sku = product.skus.order_by('id').first()
    if sku is None:
        try:
            ProductSKU.objects.create(
                product=product,
                article=codes[0],
                other_codes=codes,
            )
        except Exception as e:
            logger.warning(
                f'НК: не удалось создать ProductSKU для продукта {product.name} '
                f'(код {codes[0]}): {e}'
            )
            return False
        return True

    # Существующий SKU: обновляем только «другие коды», article не трогаем.
    if list(sku.other_codes or []) != codes:
        sku.other_codes = codes
        sku.save(update_fields=['other_codes'])
    return False


def _normalize_gtin(value: str) -> str:
    """Приводит GTIN к 14-значному формату GTIN-14."""
    value = str(value or '').strip()
    if value.isdigit() and len(value) < 14:
        return value.zfill(14)
    return value


def _parse_nc_packagings(raw_data: Dict) -> List[Dict]:
    """Извлекает информацию об упаковках из identified_by в raw_data НК.

    Использует поле level из ответа НК:
      trade-unit — потребительская упаковка (уровень 1);
      inner-pack — групповая упаковка (уровень 2);
      box / layer / pallet — транспортная упаковка (уровень 3).

    Количество вложенных потребительских кодов берётся из поля multiplier
    ответа НК (для trade-unit всегда 1; для коробки — сколько штук в ней).

    Возвращает список словарей:
        [{'level': 1, 'gtin': '...', 'quantity_inside': 1}, ...]
    """
    packagings = []
    seen_gtins = set()

    for identified in raw_data.get('identified_by') or []:
        if identified.get('type') != 'gtin':
            continue
        gtin = _normalize_gtin(identified.get('value', ''))
        level_name = identified.get('level', '') or ''
        multiplier = int(identified.get('multiplier') or 1) or 1

        if not gtin or gtin in seen_gtins:
            continue

        # Определяем уровень упаковки по уровню из НК.
        if level_name == 'trade-unit':
            level = PackagingLevelChoices.UNIT
        elif level_name in ('inner-pack', 'box'):
            level = PackagingLevelChoices.GROUP
        elif level_name in ('layer', 'pallet'):
            level = PackagingLevelChoices.TRANSPORT
        else:
            level = PackagingLevelChoices.UNIT

        packagings.append({
            'level': level,
            'gtin': gtin,
            'quantity_inside': multiplier,
        })
        seen_gtins.add(gtin)

    return packagings


def _map_product_group(nk_product_group: str) -> str:
    """Маппинг кода товарной группы НК → ProductGroupChoices."""
    mapping = {
        'milk': 'milk',
        'bio': 'bio',
    }
    return mapping.get(nk_product_group, 'null')


def sync_nk_to_products(
    user=None,
    progress: Optional[Dict] = None,
) -> Dict[str, int]:
    """Синхронизирует Product и ProductPackaging из NationalCatalogProduct.

    Логика:
    - Берём все товары из НК, готовые к производству (is_ready_for_production).
    - Если Product с таким GTIN (consumer packaging) уже существует — обновляем
      name, card_status, item_condition из НК.
    - Если Product с привязкой к данному NationalCatalogProduct уже существует —
      обновляем данные.
    - Если ничего нет — создаём Product + ProductPackaging из данных НК.

    Возвращает статистику:
    {'products_created', 'products_updated', 'packagings_created', 'skus_created'}.
    """
    nk_products = NationalCatalogProduct.objects.filter(
        card_state=CardStateChoices.PUBLISHED,
        state_condition__in=(
            StateConditionChoices.READY_ORDER_KM,
            StateConditionChoices.READY_COMMERCIALIZATION,
        ),
    ).order_by('good_id')

    if progress is not None:
        progress['total'] = nk_products.count()
        progress['done'] = 0
        progress['products_created'] = 0
        progress['products_updated'] = 0
        progress['packagings_created'] = 0
        progress['skus_created'] = 0

    products_created = 0
    products_updated = 0
    packagings_created = 0
    skus_created = 0
    done = 0

    for nk in nk_products:
        packagings_data = _parse_nc_packagings(nk.raw_data or {})
        gtin_values = [p['gtin'] for p in packagings_data if p['gtin']]

        # Ищем существующий Product: по national_product FK или по GTIN.
        product = None
        if nk.local_products.exists():
            product = nk.local_products.first()
        elif gtin_values:
            product = Product.objects.filter(
                packagings__gtin__in=gtin_values,
                is_active=True,
            ).first()

        product_group = _map_product_group(nk.product_group)

        if product:
            # Обновляем существующий продукт данными из НК.
            changed = False
            if product.name != nk.name:
                product.name = nk.name
                changed = True
            if product.card_status != nk.card_state:
                product.card_status = nk.card_state
                changed = True
            if product.item_condition != nk.state_condition:
                product.item_condition = nk.state_condition
                changed = True
            if product.group != product_group:
                product.group = product_group
                changed = True
            if product.national_product_id != nk.id:
                product.national_product = nk
                changed = True
            if changed:
                product.save()
                products_updated += 1
        else:
            # Создаём новый продукт из данных НК.
            product = Product.objects.create(
                group=product_group,
                name=nk.name or f'Товар НК #{nk.good_id}',
                shelf_life_in_days=30,
                item_condition=nk.state_condition or StateConditionChoices.NOT_READY_ORDER_KM,
                card_status=nk.card_state or CardStateChoices.DRAFT,
                national_product=nk,
            )
            products_created += 1

        # Создаём/обновляем ProductSKU из кодов учётной системы поставщика
        # (только при отсутствии номенклатуры; article при синхронизации не меняется).
        if _sync_product_sku(product, nk):
            skus_created += 1

        # Создаём/обновляем упаковки.
        # Идентифицируем упаковку по GTIN (он уникален), а не по уровню:
        # у товара может быть несколько упаковок одного уровня
        # (например, две коробки разной вместимости).
        for pkg_data in packagings_data:
            gtin = pkg_data['gtin']
            if not gtin:
                continue
            _, created = ProductPackaging.objects.update_or_create(
                product=product,
                gtin=gtin,
                defaults={
                    'level': pkg_data['level'],
                    'quantity_inside': pkg_data.get('quantity_inside', 1),
                    'code_storage_period_in_days': 30,
                },
            )
            if created:
                packagings_created += 1

        done += 1
        if progress is not None:
            progress['done'] = done
            progress['products_created'] = products_created
            progress['products_updated'] = products_updated
            progress['packagings_created'] = packagings_created
            progress['skus_created'] = skus_created

    logger.info(
        f'Синхронизация Product из НК: создано {products_created}, '
        f'обновлено {products_updated}, упаковок создано {packagings_created}, '
        f'SKU создано {skus_created}'
    )
    return {
        'products_created': products_created,
        'products_updated': products_updated,
        'packagings_created': packagings_created,
        'skus_created': skus_created,
    }
