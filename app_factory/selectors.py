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
