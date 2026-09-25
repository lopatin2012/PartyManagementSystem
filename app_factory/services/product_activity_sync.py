# app_factory/services/product_activity_sync.py

"""
Синхронизация продуктов/SKU с серверами «Молвест.Маркировка».

Для каждого действующего завода с заданным ip/port запрашивается список
продуктов (`GET {url}/workshop/api/v1/product-list/`). По каждому продукту
завода (`uuid_str` — это id нашего Product):

- заполняется `ProductSKU.sku_links` записью
  `{molvest_uuid, gtin, article, factory, active}` (артикул уникален на завод);
- создаются недостающие SKU по артикулам (`code`) — один SKU на артикул;
- активность SKU и продукта приводится к данным Молвест (это источник истины):
  артикул активен → SKU активен; продукт активен, если активен хотя бы один
  его SKU на каком-либо заводе.

В Молвест.Маркировка ничего не пишется — только чтение.
"""

import logging
from datetime import datetime, timezone as dt_timezone

from app_factory.models import Factory, Product, ProductSKU
from app_factory.services.molvest_reference_sync import fetch_factory_products

logger = logging.getLogger(__name__)


def _factory_url(factory: Factory) -> str:
    return f'http://{factory.ip_address}:{factory.port_address}'


def _now_iso() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def _build_item_map(data: list) -> dict:
    """
    Разбирает product-list завода: {article: item}.

    Артикул (`code`) уникален на завод. Несколько записей с одним артикулом
    теоретически возможны — берём первую.
    """
    items = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        code = (item.get('code') or '').strip()
        if code and code not in items:
            items[code] = item
    return items


def _upsert_link(sku: ProductSKU, factory: Factory, item: dict) -> None:
    """Обновляет/добавляет запись Молвест в sku_links SKU (по заводу)."""
    links = list(sku.sku_links or [])
    record = {
        'system': 'molvest',
        'molvest_uuid': (item.get('uuid_str') or '').strip() or None,
        'gtin': (item.get('gtin') or '').strip() or None,
        'article': (item.get('code') or '').strip() or sku.article,
        'factory': str(factory.id),
        'factory_name': factory.name,
        'active': bool(item.get('active', True)),
        'updated_at': _now_iso(),
    }
    for i, link in enumerate(links):
        if str(link.get('factory')) == str(factory.id):
            links[i] = record
            break
    else:
        links.append(record)
    sku.sku_links = links
    sku.save(update_fields=['sku_links'])


def _find_or_create_sku(product: Product, article: str) -> ProductSKU:
    """Возвращает SKU по артикулу, создавая его при отсутствии."""
    sku = ProductSKU.objects.filter(article=article).first()
    if sku is not None:
        return sku
    return ProductSKU.objects.create(product=product, article=article)


# «Живые» статусы УИП: пока УИП в них, его SKU/продукт не деактивируем.
LIVE_UIP_STATUSES = ('reserved_cz', 'reserved_local', 'registered')


def _protected_product_ids() -> set:
    """
    ID продуктов, у которых есть «живой» УИП (reserved_*/registered).

    Такие продукты/SKU не деактивируются по данным Молвест — только лог,
    чтобы не сломать резерв/производство.
    """
    from app_uip.models import UIP

    return set(
        UIP.objects.filter(status__in=LIVE_UIP_STATUSES)
        .values_list('product_sku__product_id', flat=True)
    )


def sync_product_activity(factory_ids: list = None) -> dict:
    """
    Синхронизирует продукты/SKU с product-list заводов.

    :param factory_ids: опционально — ограничить список заводов.
    :return: сводка выполнения.
    """
    summary = {
        'is_error': False,
        'factories': 0,
        'failed_factories': [],
        'skus_created': 0,
        'skus_activated': 0,
        'skus_deactivated': 0,
        'skus_protected': 0,
        'products_activated': 0,
        'products_deactivated': 0,
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
                f'Синхронизация продуктов: завод «{factory.name}» недоступен ({url}).'
            )
            continue

        item_map = _build_item_map(data)
        created = activated = deactivated = protected = 0
        protected_product_ids = _protected_product_ids()

        for article, item in item_map.items():
            # Продукт завода — по uuid_str (это id нашего Product).
            product = None
            molvest_uuid = (item.get('uuid_str') or '').strip()
            if molvest_uuid:
                product = Product.objects.filter(id=molvest_uuid).first()

            sku = ProductSKU.objects.filter(article=article).first()
            if sku is None:
                if product is None:
                    # Нет продукта для привязки — создать SKU нельзя.
                    continue
                sku = _find_or_create_sku(product, article)
                created += 1
                need_active = True
            else:
                need_active = sku.is_active

            _upsert_link(sku, factory, item)

            active = bool(item.get('active', True))
            if active and not sku.is_active:
                ProductSKU.objects.filter(id=sku.id).update(is_active=True)
                activated += 1
            elif not active and sku.is_active:
                # Мягкая защита: не выключаем SKU продукта с «живым» УИП.
                if sku.product_id in protected_product_ids:
                    protected += 1
                    logger.warning(
                        f'Синхронизация: SKU {article} в Молвест неактивен, '
                        f'но у продукта есть активный УИП — деактивация пропущена.'
                    )
                else:
                    ProductSKU.objects.filter(id=sku.id).update(is_active=False)
                    deactivated += 1

        # Продукт активен, если активен хотя бы один его SKU.
        touched_products = {
            s.product_id
            for s in ProductSKU.objects.filter(article__in=list(item_map.keys()))
        }
        products_activated, products_deactivated = _recompute_products(
            touched_products, protected_product_ids
        )

        summary['factories'] += 1
        summary['skus_created'] += created
        summary['skus_activated'] += activated
        summary['skus_deactivated'] += deactivated
        summary['skus_protected'] += protected
        summary['products_activated'] += products_activated
        summary['products_deactivated'] += products_deactivated
        summary['details'][str(factory.id)] = {
            'fetched': len(data),
            'articles': len(item_map),
            'skus_created': created,
            'skus_activated': activated,
            'skus_deactivated': deactivated,
            'skus_protected': protected,
        }
        logger.info(
            f'Синхронизация продуктов завода «{factory.name}»: '
            f'артикулов {len(item_map)}, SKU создано {created}, '
            f'вкл {activated}, выкл {deactivated}.'
        )

    # Итоговая пересборка активности всех затронутых продуктов.
    summary['message'] = (
        f'Синхронизация продуктов: заводов {summary["factories"]}, '
        f'SKU создано {summary["skus_created"]}, '
        f'SKU вкл {summary["skus_activated"]}, выкл {summary["skus_deactivated"]}, '
        f'защищено {summary["skus_protected"]}, '
        f'продуктов вкл {summary["products_activated"]}, '
        f'выкл {summary["products_deactivated"]}, '
        f'ошибок {len(summary["failed_factories"])}.'
    )
    logger.info(summary['message'])
    return summary


def _recompute_products(product_ids, protected_ids=None) -> tuple[int, int]:
    """Активность продукта = есть ли активный SKU. Возвращает (вкл, выкл)."""
    protected_ids = protected_ids or set()
    activated = deactivated = 0
    for product in Product.objects.filter(id__in=product_ids):
        has_active = ProductSKU.objects.filter(
            product=product, is_active=True
        ).exists()
        if has_active and not product.is_active:
            Product.objects.filter(id=product.id).update(is_active=True)
            activated += 1
        elif (not has_active and product.is_active
              and product.id not in protected_ids):
            Product.objects.filter(id=product.id).update(is_active=False)
            deactivated += 1
    return activated, deactivated
