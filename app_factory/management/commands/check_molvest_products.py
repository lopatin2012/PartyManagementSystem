# app_factory/management/commands/check_molvest_products.py

"""
Отчёт о расхождениях между СУП и «Молвест.Маркировка» (только чтение).

Читает `product-list` всех активных заводов и показывает:
- GTIN, встречающиеся у РАЗНЫХ продуктов Молвест (`uuid_str`) — дубли
  потребительской упаковки;
- артикулы Молвест, которых нет в СУП (SKU не создан);
- артикулы, привязанные в Молвест к другому `uuid_str`, чем наш Product.

В Молвест ничего не пишется.

Пример:
    python manage.py check_molvest_products
    python manage.py check_molvest_products --factory-id <uuid>
"""

import logging
from collections import defaultdict

from django.core.management.base import BaseCommand

from app_factory.models import Factory, ProductSKU
from app_factory.services.molvest_reference_sync import fetch_factory_products

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Отчёт о расхождениях продуктов СУП и Молвест.Маркировка'

    def add_arguments(self, parser):
        parser.add_argument(
            '--factory-id',
            action='append',
            dest='factory_ids',
            help='ID завода (можно несколько). По умолчанию — все активные.',
        )

    def handle(self, *args, **options):
        factories = Factory.objects.filter(is_active=True).exclude(
            ip_address__isnull=True
        ).exclude(port_address__isnull=True)
        if options.get('factory_ids'):
            factories = factories.filter(id__in=options['factory_ids'])

        gtin_to_uuids = defaultdict(set)      # gtin -> {molvest_uuid}
        gtin_to_articles = defaultdict(list)  # gtin -> [(factory, code, uuid, active)]
        article_owner = {}                    # (factory_id, code) -> uuid
        missing_skus = []                     # (factory, code, uuid)

        for factory in factories:
            url = f'http://{factory.ip_address}:{factory.port_address}'
            data = fetch_factory_products(url)
            if data is None:
                self.stderr.write(self.style.ERROR(
                    f'Завод «{factory.name}» недоступен ({url}).'
                ))
                continue

            for item in data:
                if not isinstance(item, dict):
                    continue
                code = (item.get('code') or '').strip()
                gtin = (item.get('gtin') or '').strip()
                molvest_uuid = (item.get('uuid_str') or '').strip()
                active = bool(item.get('active', True))
                if not code:
                    continue

                if gtin:
                    gtin_to_uuids[gtin].add(molvest_uuid)
                    gtin_to_articles[gtin].append(
                        (factory.name, code, molvest_uuid, active)
                    )
                article_owner[(str(factory.id), code)] = molvest_uuid

                if not ProductSKU.objects.filter(article=code).exists():
                    missing_skus.append((factory.name, code, molvest_uuid))

        # 1. Дубли: один GTIN у разных продуктов Молвест.
        dup_gtins = {g: u for g, u in gtin_to_uuids.items() if len(u) > 1}
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'GTIN с разными продуктами Молвест (дубли): {len(dup_gtins)}'
        ))
        for gtin, uuids in list(dup_gtins.items())[:50]:
            self.stdout.write(f'  GTIN {gtin}:')
            for name, code, uu, active in gtin_to_articles[gtin]:
                self.stdout.write(
                    f'    [{name}] code={code} uuid={uu} active={active}'
                )

        # 2. Артикулы Молвест, которых нет в СУП.
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Артикулы без SKU в СУП: {len(missing_skus)}'
        ))
        for name, code, uu in missing_skus[:50]:
            self.stdout.write(f'  [{name}] code={code} uuid={uu}')

        # 3. SKU с molvest-links на другой uuid, чем в Молвест.
        mismatches = []
        for (factory_id, code), molvest_uuid in article_owner.items():
            sku = ProductSKU.objects.filter(article=code).first()
            if sku and molvest_uuid and str(sku.product_id) != molvest_uuid:
                mismatches.append((factory_id, code, molvest_uuid, str(sku.product_id)))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Несовпадение привязки SKU <-> Молвест: {len(mismatches)}'
        ))
        for factory_id, code, molvest_uuid, sup_uuid in mismatches[:50]:
            self.stdout.write(
                f'  code={code}: Молвест uuid={molvest_uuid}, СУП uuid={sup_uuid}'
            )

        self.stdout.write(self.style.SUCCESS('Отчёт завершён.'))
