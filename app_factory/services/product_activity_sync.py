# app_factory/services/product_activity_sync.py

"""
Проверка активности продуктов через серверы «Молвест.Маркировка».

Для каждого действующего завода с заданным ip/port запрашивается список
продуктов (`GET {url}/workshop/api/v1/product-list/`). Локальные SKU,
привязанные к линиям этого завода, но отсутствующие в списке или помеченные
`active=false`, деактивируются (`ProductSKU.is_active=False`). Если у продукта
не остаётся ни одного активного SKU — продукт тоже помечается неактивным.

Задача выполняется раз в сутки ДО накопления резерва УИП, чтобы в резерв
не попадала продукция, снятая с производства.

Важно: `sync_molvest_reference` выставляет `is_active` из product-list только
при СОЗДАНИИ SKU, поэтому деактивация здесь не перетирается следующей
синхронизацией справочников.
"""

import logging

from app_factory.models import Factory, Product, ProductSKU
from app_factory.services.molvest_reference_sync import fetch_factory_products

logger = logging.getLogger(__name__)


def _factory_url(factory: Factory) -> str:
    return f'http://{factory.ip_address}:{factory.port_address}'


def _active_articles(data: list) -> set:
    """Артикулы (code) продуктов, которые на заводе активны."""
    active = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        code = (item.get('code') or '').strip()
        if not code:
            continue
        if bool(item.get('active', True)):
            active.add(code)
    return active


def _deactivate_skus(skus, active_articles: set) -> int:
    """Деактивирует SKU, которых нет в active_articles. Возвращает количество."""
    to_deactivate = [sku for sku in skus if sku.article not in active_articles]
    if not to_deactivate:
        return 0

    ids = [sku.id for sku in to_deactivate]
    ProductSKU.objects.filter(id__in=ids).update(is_active=False)

    # Продукт без активных SKU тоже помечаем неактивным.
    product_ids = {sku.product_id for sku in to_deactivate}
    for product_id in product_ids:
        still_active = ProductSKU.objects.filter(
            product_id=product_id, is_active=True
        ).exists()
        if not still_active:
            Product.objects.filter(id=product_id).update(is_active=False)

    return len(to_deactivate)


def sync_product_activity(factory_ids: list = None) -> dict:
    """
    Проверяет активность продуктов по всем заводам и деактивирует снятые.

    :param factory_ids: опционально — ограничить список заводов.
    :return: сводка выполнения.
    """
    summary = {
        'is_error': False,
        'factories': 0,
        'failed_factories': [],
        'deactivated': 0,
        'details': {},
        'message': '',
    }

    factories = (
        Factory.objects.filter(is_active=True)
        .exclude(ip_address__isnull=True)
        .exclude(port_address__isnull=True)
    )
    if factory_ids:
        factories = factories.filter(id__in=factory_ids)

    if not factories.exists():
        summary['message'] = 'Нет действующих заводов с заданным ip-адресом/портом.'
        logger.warning(summary['message'])
        return summary

    for factory in factories:
        url = _factory_url(factory)
        data = fetch_factory_products(url)
        if data is None:
            summary['is_error'] = True
            summary['failed_factories'].append(str(factory.id))
            logger.warning(
                f'Проверка активности: завод «{factory.name}» недоступен ({url}).'
            )
            continue

        active_articles = _active_articles(data)

        # SKU, привязанные к линиям этого завода.
        skus = list(
            ProductSKU.objects.filter(
                product_production_locations__line__workshop__factory=factory,
                is_active=True,
            )
            .distinct()
        )
        deactivated = _deactivate_skus(skus, active_articles)

        summary['factories'] += 1
        summary['deactivated'] += deactivated
        summary['details'][str(factory.id)] = {
            'fetched': len(data),
            'checked': len(skus),
            'deactivated': deactivated,
        }
        logger.info(
            f'Проверка активности завода «{factory.name}»: '
            f'проверено {len(skus)}, деактивировано {deactivated}.'
        )

    summary['message'] = (
        f'Проверка активности продуктов: заводов {summary["factories"]}, '
        f'деактивировано SKU {summary["deactivated"]}, '
        f'ошибок {len(summary["failed_factories"])}.'
    )
    logger.info(summary['message'])
    return summary
