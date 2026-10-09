# app_uip/services/reserve_accumulation.py

"""
Накопление резерва УИП на несколько дней вперёд.

Для активных SKU обычного формата (`TypeFormationUIP.general`), у продукта
которых короткий срок годности (не более `UIP_SHORT_SHELF_LIFE_DAYS`,
по умолчанию 45 дней, включительно), поддерживается резерв зарезервированных
УИП на окно дат `[сегодня; сегодня + ProductSKU.reserve_days]`.

Правила:
* доливается всё окно — пробелов по датам не остаётся;
* на одну дату создаётся ровно один УИП; если УИП уже есть — пропускаем;
* «сгоревший» (`deleted`) УИП повторно резервируется тем же номером;
* номер формируется локально (`build_local_party_number`);
* срок годности `41..UIP_SHORT_SHELF_LIFE_DAYS` — УИП резервируются в ЧЗ
  собственным (локальным) номером (`reserved_local`);
* срок годности не более `UIP_DRAFT_SHELF_LIFE_DAYS` (по умолчанию 40) —
  создаются черновики без обращения к ЧЗ; **кроме дат производства начиная с
  `SHORT_SHELF_LIFE_RESERVE_FROM` (01.03.2027)** — они резервируются в ЧЗ
  (только автоматическое накопление; ручная генерация это правило не
  применяет); при явном `skip_cz=False` такие УИП тоже резервируются в ЧЗ;
* при резервировании в ЧЗ, если заполнение резерва превышает `RELEASE_PERCENT`
  (95%) — резервирование пропускается, чтобы не ухудшать ситуацию.
"""

import logging
import time
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from app_cz.services.party_service import (
    build_local_party_number,
    reserve_parties_honest_sign,
    restore_burned_uips,
)
from app_cz.services.reserve_monitor import RELEASE_PERCENT, get_reserve_stats
from app_factory.models import ProductSKU, TypeFormationUIP
from app_uip.models import PartyStatusChoices, UIP, UIPStatusLog

logger = logging.getLogger(__name__)

# Размер пачки резервирования в ЧЗ и пауза между запросами (секунды).
CZ_BATCH_SIZE = 50
CZ_BATCH_PAUSE_SECONDS = 10

# Формат УИП, для которого поддерживается накопление.
RESERVE_TYPE_FORMATION = TypeFormationUIP.general

# С этой даты продукция с коротким сроком годности (не более
# UIP_DRAFT_SHELF_LIFE_DAYS) тоже резервируется в ЧЗ. До неё — черновик.
# Правило действует только для автоматического накопления.
SHORT_SHELF_LIFE_RESERVE_FROM = date(2027, 3, 1)


def _short_shelf_life_days() -> int:
    """Порог срока годности (дней, включительно), до которого накапливаем резерв."""
    return int(getattr(settings, 'UIP_SHORT_SHELF_LIFE_DAYS', 45) or 45)


def _draft_shelf_life_days() -> int:
    """Порог срока годности (дней, включительно), до которого создаём черновики."""
    return int(getattr(settings, 'UIP_DRAFT_SHELF_LIFE_DAYS', 40) or 40)


def should_reserve_in_cz(sku) -> bool:
    """
    Должен ли УИП этого SKU резервироваться в ЧЗ по сроку годности продукта.

    Продукция со сроком годности больше `UIP_DRAFT_SHELF_LIFE_DAYS` (по
    умолчанию 40 дней) резервируется в ЧЗ; более короткий срок — черновик.
    """
    return sku.product.shelf_life_in_days > _draft_shelf_life_days()


def _should_reserve_entry(entry, skip_cz) -> bool:
    """
    Резервировать ли конкретный УИП (SKU + дата производства) в ЧЗ.

    * срок годности > `UIP_DRAFT_SHELF_LIFE_DAYS` (в пределах
      `UIP_SHORT_SHELF_LIFE_DAYS`) — всегда резерв в ЧЗ;
    * короткий срок, но дата производства >= `SHORT_SHELF_LIFE_RESERVE_FROM`
      (01.03.2027) — тоже резерв в ЧЗ (только при автопополнении);
    * иначе — черновик, если не передан явный `skip_cz=False`.
    """
    if should_reserve_in_cz(entry['sku']):
        return True
    production_date = entry.get('date')
    if production_date and production_date >= SHORT_SHELF_LIFE_RESERVE_FROM:
        return True
    return skip_cz is False


def _short_shelf_life_skus():
    """
    Активные SKU обычного формата в пределах срока годности накопления
    (черновики и резерв в ЧЗ).
    """
    return (
        ProductSKU.objects.filter(
            is_active=True,
            product__is_active=True,
            product__shelf_life_in_days__lte=_short_shelf_life_days(),
            type_formation_uip=RESERVE_TYPE_FORMATION,
        )
        .select_related('product')
        .order_by('article')
    )


def _plan_reserve(skus) -> list[dict]:
    """
    Строит план накопления по окну дат.

    :return: список словарей {'sku', 'date', 'number', 'action'}, где action —
             'create' (УИП нет), 'restore' (сгорел) или 'skip' (уже есть).
    """
    today = timezone.now().date()

    candidates = []
    for sku in skus:
        gtin = sku.product.consumer_gtin
        if not gtin:
            continue
        days = sku.reserve_days or 0
        for offset in range(0, days + 1):
            production_date = today + timedelta(days=offset)
            number = build_local_party_number(
                gtin,
                production_date,
                article=sku.article,
                party='000',
                type_formation_uip=sku.type_formation_uip,
            )
            if not number:
                continue
            candidates.append({
                'sku': sku,
                'date': production_date,
                'number': number,
            })

    if not candidates:
        return []

    existing = {
        uip.number: uip.status
        for uip in UIP.objects.filter(
            number__in=[c['number'] for c in candidates]
        )
    }

    for candidate in candidates:
        status = existing.get(candidate['number'])
        if status is None:
            candidate['action'] = 'create'
        elif status == PartyStatusChoices.DELETED:
            candidate['action'] = 'restore'
        else:
            candidate['action'] = 'skip'

    return candidates


def _reserve_numbers(entries: list[dict], pause_seconds: float) -> tuple[list, list]:
    """
    Резервирует номера в ЧЗ пачками (в разрезе товарных групп).

    :return: (успешно зарезервированные entries, список ошибок).
    """
    by_group: dict = {}
    for entry in entries:
        by_group.setdefault(entry['sku'].product.group, []).append(entry)

    reserved = []
    errors = []
    first_request = True

    for group, group_entries in by_group.items():
        for start in range(0, len(group_entries), CZ_BATCH_SIZE):
            chunk = group_entries[start:start + CZ_BATCH_SIZE]
            numbers = [e['number'] for e in chunk]

            if not first_request and pause_seconds:
                time.sleep(pause_seconds)
            first_request = False

            result = reserve_parties_honest_sign(
                product_group=group,
                party_numbers=numbers,
            )
            if result.get('is_error'):
                error = result.get('message_error', 'Ошибка резервирования в ЧЗ')
                errors.append(error)
                logger.warning(
                    f'Накопление резерва: не удалось зарезервировать '
                    f'{len(numbers)} номер(ов): {error}'
                )
                continue

            reserved.extend(chunk)
            logger.info(
                f'Накопление резерва: зарезервировано {len(numbers)} номер(ов) '
                f'в ЧЗ (группа {group}).'
            )

    return reserved, errors


def _create_uip(entry: dict, skip_cz: bool) -> None:
    """Создаёт локальный УИП (черновик или зарезервированный)."""
    sku = entry['sku']
    if skip_cz:
        status = PartyStatusChoices.DRAFT
        reservation_date = None
        note = 'Автоматическое накопление резерва УИП (черновик, без ЧЗ)'
    else:
        status = PartyStatusChoices.RESERVED_LOCAL
        reservation_date = timezone.now().date()
        note = 'Автоматическое накопление резерва УИП'

    with transaction.atomic():
        uip = UIP.objects.create(
            product_sku=sku,
            number=entry['number'],
            status=status,
            production_date=entry['date'],
            reservation_date=reservation_date,
            description=note,
        )
        UIPStatusLog.objects.create(
            uip=uip,
            from_status=None,
            to_status=status,
            source='service',
            note=note,
        )


def _persist_entries(entries: list[dict], skip_cz: bool) -> tuple[int, int]:
    """Сохраняет номера локально: создаёт или восстанавливает сгоревшие."""
    created = 0
    restored = 0
    for entry in entries:
        try:
            if entry['action'] == 'create':
                _create_uip(entry, skip_cz=skip_cz)
                created += 1
            elif entry['action'] == 'restore' and not skip_cz:
                if restore_burned_uips(
                    [entry['number']],
                    target_status=PartyStatusChoices.RESERVED_LOCAL,
                    source='service',
                ):
                    restored += 1
        except Exception as exc:  # noqa: BLE001 — один сбой не должен ронять прогон
            logger.error(
                f'Накопление резерва: ошибка сохранения УИП '
                f'{entry["number"]}: {exc}',
                exc_info=True,
            )
    return created, restored


def accumulate_short_shelf_life_reserve(
        pause_seconds: float = None,
        skip_cz: bool = None,
) -> dict:
    """
    Доливает резерв УИП на окно дат для короткоживущей/средней продукции.

    Продукция со сроком годности `41..UIP_SHORT_SHELF_LIFE_DAYS` резервируется
    в ЧЗ собственным (локальным) номером. Для срока годности не более
    `UIP_DRAFT_SHELF_LIFE_DAYS` создаются черновики; если передан явный
    `skip_cz=False`, такие УИП тоже резервируются в ЧЗ.

    :param pause_seconds: пауза между запросами в ЧЗ (по умолчанию
                          CZ_BATCH_PAUSE_SECONDS).
    :param skip_cz: явное решение для продукции со сроком годности не более
                    `UIP_DRAFT_SHELF_LIFE_DAYS`: False — резервировать в ЧЗ,
                    None/True — черновик. На более длинный срок не влияет.
    :return: сводка выполнения.
    """
    if pause_seconds is None:
        pause_seconds = CZ_BATCH_PAUSE_SECONDS

    skus = list(_short_shelf_life_skus())
    plan = _plan_reserve(skus)
    to_process = [e for e in plan if e['action'] in ('create', 'restore')]
    skipped_existing = sum(1 for e in plan if e['action'] == 'skip')

    if not to_process:
        message = (
            'Накопление резерва УИП: все УИП уже существуют, доливать нечего.'
        )
        logger.info(message)
        return {
            'is_error': False,
            'skipped': False,
            'reserved': 0,
            'drafted': 0,
            'created': 0,
            'restored': 0,
            'skipped_existing': skipped_existing,
            'failed': 0,
            'errors': [],
            'message': message,
        }

    reserve_entries = [
        e for e in to_process if _should_reserve_entry(e, skip_cz)
    ]
    draft_entries = [
        e for e in to_process if not _should_reserve_entry(e, skip_cz)
    ]

    # Резервирование в ЧЗ не запускаем, если резерв уже переполнен.
    errors = []
    skipped_full = False
    reserve_total = len(reserve_entries)
    if reserve_entries:
        stats = get_reserve_stats()
        if stats['percent'] > RELEASE_PERCENT:
            skipped_full = True
            reserve_entries = []
            logger.warning(
                f'Накопление резерва: резервирование в ЧЗ пропущено, '
                f'заполнение {stats["percent"]}% превышает порог '
                f'{RELEASE_PERCENT}% ({stats["count"]}/{stats["limit"]}).'
            )

    if reserve_entries:
        reserved, reserve_errors = _reserve_numbers(reserve_entries, pause_seconds)
        errors.extend(reserve_errors)
    else:
        reserved = []

    created, restored = _persist_entries(draft_entries, skip_cz=True)
    reserved_created, reserved_restored = _persist_entries(reserved, skip_cz=False)
    created += reserved_created
    restored += reserved_restored

    failed = reserve_total - len(reserved)

    message = (
        f'Накопление резерва УИП (черновиков {len(draft_entries)}, '
        f'резерв ЧЗ {len(reserved)}): создано {created}, '
        f'восстановлено {restored}, пропущено (уже есть) {skipped_existing}, '
        f'не зарезервировано {failed} (из {len(to_process)} к обработке).'
    )
    if skipped_full:
        message += ' Резерв ЧЗ переполнен — резервирование пропущено.'
    logger.info(message)
    return {
        'is_error': bool(errors),
        'skipped': skipped_full,
        'reason': 'reserve_full' if skipped_full else None,
        'created': created,
        'restored': restored,
        'reserved': len(reserved),
        'drafted': len(draft_entries),
        'skipped_existing': skipped_existing,
        'failed': failed,
        'errors': errors,
        'message': message,
    }
