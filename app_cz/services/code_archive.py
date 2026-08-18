# app_cz\services\code_archive.py

import logging
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from app_cz.models import CISCode, CISCodeArchive

logger = logging.getLogger(__name__)

# Возраст кода (в днях), после которого он подлежит переносу в архив.
ARCHIVE_AFTER_DAYS = 45

# Размер батча для переноса (за один проход).
ARCHIVE_BATCH_SIZE = 5000


def _build_archive_rows(codes: list) -> list:
    """Строит денормализованные архивные записи из кодов."""
    rows = []
    for c in codes:
        party = c.production_party
        uip = party.uip if party else None
        packaging = c.product_packaging
        product = packaging.product if packaging else None
        skus = product.skus.all() if product else []
        sku = skus[0] if skus else None

        rows.append(CISCodeArchive(
            id=c.id,
            code=c.code,
            level=c.level,
            cz_status=c.cz_status,
            production_status=c.production_status,
            parent_code=c.parent.code if c.parent else None,
            production_party_id=party.id if party else None,
            party_number=party.production_party if party else None,
            party_status=party.status if party else None,
            uip_id=uip.id if uip else None,
            uip_number=uip.number if uip else None,
            product_id=product.id if product else None,
            product_name=product.name if product else None,
            product_sku_id=sku.id if sku else None,
            sku_article=sku.article if sku else None,
            packaging_id=packaging.id if packaging else None,
            gtin=packaging.gtin if packaging else None,
            created_at=c.created_at,
            updated_at=c.updated_at,
        ))
    return rows


def _delete_from_main(ids: list) -> None:
    """
    Удаляет коды из рабочей таблицы.

    Используем сырой DELETE: FK parent (ON DELETE SET NULL) обрабатывается
    на уровне СУБД, поэтому удаление не порождает N+1 UPDATE'ов.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            'DELETE FROM app_cz_ciscode WHERE id = ANY(%s)',
            [list(ids)],
        )


def archive_old_codes(
        days: int = ARCHIVE_AFTER_DAYS,
        batch_size: int = ARCHIVE_BATCH_SIZE,
        limit: int = 0,
        dry_run: bool = False,
) -> dict:
    """
    Переносит устаревшие коды маркировки в архивную базу.

    Критерий: created_at младше (now - days). Коды не удаляются безвозвратно —
    они переносятся в архив (историчность сохраняется). Чистка архива —
    вручную.

    :param days: Возраст кода в днях (по умолчанию 45).
    :param batch_size: Размер батча.
    :param limit: Максимум кодов за запуск (0 — без ограничения).
    :param dry_run: Только подсчёт, без переноса и удаления.
    :return: Сводка по архивации.
    """
    cutoff = timezone.now() - timedelta(days=days)
    summary = {
        'is_error': False,
        'days': days,
        'cutoff': cutoff.isoformat(),
        'candidates': 0,
        'archived': 0,
        'already_in_archive': 0,
        'deleted': 0,
        'errors': 0,
        'dry_run': dry_run,
        'message': '',
    }

    archived_total = 0
    processed_total = 0
    already_total = 0

    while True:
        if limit and archived_total >= limit:
            break

        batch_limit = min(batch_size, limit - archived_total) if limit else batch_size

        qs = (
            CISCode.objects
            .filter(created_at__lt=cutoff)
            .select_related(
                'production_party',
                'production_party__uip',
                'product_packaging',
                'product_packaging__product',
                'parent',
            )
            .prefetch_related('product_packaging__product__skus')
            .order_by('created_at', 'id')[:batch_limit]
        )
        codes = list(qs)
        if not codes:
            break

        codes_ids = [c.id for c in codes]
        already_ids = set(
            CISCodeArchive.objects.using('archive')
            .filter(id__in=codes_ids)
            .values_list('id', flat=True)
        )
        to_archive = [c for c in codes if c.id not in already_ids]

        already_total += len(already_ids)

        if not dry_run:
            if to_archive:
                rows = _build_archive_rows(to_archive)
                try:
                    CISCodeArchive.objects.using('archive').bulk_create(
                        rows,
                        batch_size=batch_size,
                        ignore_conflicts=True,
                    )
                except Exception as e:
                    logger.exception(f'Ошибка переноса кодов в архив: {e}')
                    summary['errors'] += len(to_archive)
                    break
                archived_total += len(rows)

            # Удаляем из рабочей таблицы перенесённые и уже бывшие в архиве.
            try:
                _delete_from_main(codes_ids)
            except Exception as e:
                logger.exception(f'Ошибка удаления кодов из рабочей таблицы: {e}')
                summary['errors'] += len(codes_ids)
                break
        else:
            archived_total += len(to_archive)

        processed_total += len(codes)

    summary['candidates'] = processed_total
    summary['archived'] = archived_total
    summary['already_in_archive'] = already_total
    summary['deleted'] = processed_total if not dry_run else 0
    summary['is_error'] = summary['errors'] > 0

    mode = 'СУХОЙ ПРОГОН' if dry_run else 'архивация'
    parts = [
        f'{mode}: кандидатов {summary["candidates"]}',
        f'перенесено {summary["archived"]}',
        f'уже в архиве {summary["already_in_archive"]}',
        f'удалено из рабочей {summary["deleted"]}',
    ]
    if summary['errors']:
        parts.append(f'⚠ ошибок: {summary["errors"]}')
    summary['message'] = 'Коды старше {days} дн. ({cutoff}). {parts}'.format(
        days=days, cutoff=cutoff.date(), parts=', '.join(parts)
    )

    logger.info(summary['message'])
    return summary