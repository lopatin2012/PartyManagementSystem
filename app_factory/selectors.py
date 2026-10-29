# app_factory/selectors.py

from uuid import UUID
from typing import Optional, Dict

from django.db.models import Prefetch

from app_factory.models import Product, ProductPackaging, PackagingLevelChoices

def get_products_with_gtin_map() -> Dict[UUID, Dict[int, str]]:
    """Возвращает словарь: {product_id: {level: gtin, ...}}"""
    products = Product.objects.filter(is_active=True).prefetch_related(
        Prefetch('packagings', queryset=ProductPackaging.objects.filter(is_active=True))
    )

    gtin_map = {}
    for product in products:
        gtin_map[product.id] = {
            p.level: p.gtin for p in product.packagings.all()
        }
    return gtin_map

def get_product_consumer_gtin(product_id: UUID) -> Optional[str]:
    """Возвращает GTIN потребительской упаковки для одного продукта."""
    product = Product.objects.filter(id=product_id, is_active=True).prefetch_related(
        Prefetch(
            'packagings',
            queryset=ProductPackaging.objects.filter(
                is_active=True,
                level=PackagingLevelChoices.UNIT
            )
        )
    ).first()

    if not product:
        return None

    packaging = product.packagings.first()
    return packaging.gtin if packaging else None


def sku_link_priority(sku) -> int:
    """
    Приоритет SKU по записям `ProductSKU.sku_links`.

    0 — есть активная привязка к внешней системе (артикул используется
    заводом), 1 — привязка есть, но неактивная, 2 — привязок нет.
    """
    links = [link for link in (sku.sku_links or []) if isinstance(link, dict)]
    if not links:
        return 2
    if any(link.get('active', True) for link in links):
        return 0
    return 1


def prefer_linked_skus(skus) -> list:
    """
    Упорядочивает SKU: сначала привязанные к внешней системе (Молвест).

    У продукта может быть несколько артикулов на один GTIN — старый (без
    записи в `sku_links`) и актуальный, который завод указывает в заданиях
    «Молвест.Маркировка» (запись в `sku_links`). Просто первый по артикулу
    может оказаться неактуальным, поэтому привязка важнее алфавита.
    """
    return sorted(
        skus,
        key=lambda sku: (sku_link_priority(sku), not sku.is_active, sku.article),
    )


def find_sku_for_factory(product, factory, article_prefix: str = ''):
    """
    SKU продукта, привязанный к указанному заводу в `ProductSKU.sku_links`.

    У продукта может быть несколько артикулов на один GTIN (например, после
    смены пластинки завод заводит новый `code`). Актуальный артикул задания —
    тот, чья запись `sku_links` ссылается на завод задания (по id или имени).

    Если к заводу привязано несколько артикулов, `article_prefix` (первые
    5 символов артикула из задания/номера УИП) выбирает нужный: без него
    победил бы первый по алфавиту, что может быть устаревшим артикулом.

    :return: ProductSKU или None.
    """
    if not product or not factory:
        return None

    factory_id = str(factory.id)
    factory_name = str(getattr(factory, 'name', '') or '').strip()
    candidates = []

    for sku in product.skus.all():
        for link in (sku.sku_links or []):
            if not isinstance(link, dict):
                continue
            linked_id = str(link.get('factory') or '').strip()
            linked_name = str(link.get('factory_name') or '').strip()
            if linked_id != factory_id and linked_name != factory_name:
                continue
            candidates.append((bool(link.get('active', True)), sku))
            break

    if not candidates:
        return None

    prefix = (article_prefix or '').strip()[:5]
    if prefix:
        matching = [
            (is_active, sku) for is_active, sku in candidates
            if sku.article[:5] == prefix
        ]
        if matching:
            candidates = matching

    # Активная привязка и активный SKU — в приоритете, затем по артикулу.
    active = [sku for is_active, sku in candidates if is_active]
    pool = active or [sku for _, sku in candidates]
    pool.sort(key=lambda sku: (not sku.is_active, sku.article))
    return pool[0]
