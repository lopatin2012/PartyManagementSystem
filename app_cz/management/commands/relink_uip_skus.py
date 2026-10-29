# app_cz/management/commands/relink_uip_skus.py


"""
Перепривязка УИП к актуальному артикулу завода.

У продукта может быть несколько SKU (артикулов) на один GTIN: старый и
актуальный, который завод указывает в заданиях «Молвест.Маркировка».
Привязка артикула к заводу хранится в `ProductSKU.sku_links`. Если УИП
привязан к устаревшему артикулу, на /cz/sync показывается артикул, который
в заданиях не используется.

Команда смотрит завод последнего задания по каждому УИП и перепривязывает
УИП к SKU этого завода (если для завода есть привязка в `sku_links`).
Если к заводу привязано несколько артикулов одного GTIN, выбирается тот,
чей артикул совпадает с артикулом из номера УИП (GTIN + дата + артикул).

Примеры:
    python manage.py relink_uip_skus --dry-run
    python manage.py relink_uip_skus
    python manage.py relink_uip_skus --article 91229 --dry-run
    python manage.py relink_uip_skus --limit 1000
"""

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from app_factory.selectors import find_sku_for_factory
from app_uip.models import ProductionParty, UIP
from app_cz.services.party_service import article_prefix_from_local_number


def _latest_parties_with_factory():
    """Партии УИП, у которых известен завод (линия → цех → завод)."""
    return Prefetch(
        'production_parties',
        queryset=(
            ProductionParty.objects
            .filter(line__workshop__factory__isnull=False)
            .select_related('line__workshop__factory')
            .order_by('-created_at', '-id')
        ),
        to_attr='parties_with_factory',
    )


def relink_uip_skus(article: str = '', number: str = '', limit: int = 0,
                    dry_run: bool = False) -> dict:
    """
    Перепривязывает УИП к SKU завода последнего задания.

    :param article: только УИП с текущим артикулом (например, '91229').
    :param number: только УИП с указанным номером.
    :param limit: максимум УИП за запуск (0 — без ограничения).
    :param dry_run: показать изменения, ничего не сохраняя.
    :return: сводка.
    """
    result = {
        'checked': 0,
        'relinked': 0,
        'no_factory': 0,
        'no_linked_sku': 0,
        'changes': [],
        'dry_run': dry_run,
        'message': '',
    }

    queryset = (
        UIP.objects
        .filter(product_sku__isnull=False)
        .select_related('product_sku__product')
        .prefetch_related(_latest_parties_with_factory())
        .order_by('number')
    )
    if article:
        queryset = queryset.filter(product_sku__article=article)
    if number:
        queryset = queryset.filter(number=number)

    uips = []
    for uip in queryset.iterator(chunk_size=500):
        result['checked'] += 1
        uips.append(uip)
        if limit and result['checked'] >= limit:
            break

    for uip in uips:
        parties = getattr(uip, 'parties_with_factory', [])
        if not parties:
            result['no_factory'] += 1
            continue

        factory = parties[0].line.workshop.factory
        sku = find_sku_for_factory(
            uip.product_sku.product,
            factory,
            article_prefix=article_prefix_from_local_number(uip.number),
        )
        if sku is None:
            result['no_linked_sku'] += 1
            continue

        if sku.id == uip.product_sku_id:
            continue

        old_article = uip.product_sku.article
        result['relinked'] += 1
        result['changes'].append(
            f'{uip.number}: {old_article} -> {sku.article} '
            f'(завод «{factory.name}»)'
        )
        if not dry_run:
            uip.product_sku = sku
            uip.save(update_fields=['product_sku', 'updated_at'])

    prefix = 'dry-run: ' if dry_run else ''
    result['message'] = (
        f'{prefix}проверено УИП {result["checked"]}, '
        f'перепривязано {result["relinked"]}, '
        f'без завода {result["no_factory"]}, '
        f'без привязки артикула к заводу {result["no_linked_sku"]}'
    )
    return result


class Command(BaseCommand):
    help = 'Перепривязывает УИП к артикулу, привязанному к заводу задания'

    def add_arguments(self, parser):
        parser.add_argument(
            '--article',
            default='',
            help='Только УИП с текущим артикулом (например, 91229)',
        )
        parser.add_argument(
            '--number',
            default='',
            help='Только УИП с указанным номером',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Максимум УИП за запуск (0 — без ограничения)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Показать изменения, ничего не сохраняя',
        )

    def handle(self, *args, **options):
        result = relink_uip_skus(
            article=options['article'],
            number=options['number'],
            limit=options['limit'],
            dry_run=options['dry_run'],
        )

        for change in result['changes']:
            self.stdout.write(f'  {change}')

        if result['dry_run']:
            self.stdout.write(self.style.WARNING(result['message']))
        else:
            self.stdout.write(self.style.SUCCESS(result['message']))
