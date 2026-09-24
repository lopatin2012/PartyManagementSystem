# app_uip/services/reserve_accumulation.py

"""
Накопление резерва УИП на несколько дней вперёд.

Для активных SKU обычного формата (`TypeFormationUIP.general`), у продукта
которых короткий срок годности (менее `UIP_SHORT_SHELF_LIFE_DAYS`, по умолчанию
40 дней), поддерживается резерв зарезервированных УИП на окно дат
`[сегодня; сегодня + ProductSKU.reserve_days]`.

Правила:
* доливается всё окно — пробелов по датам не остаётся;
* на одну дату создаётся ровно один УИП; если УИП уже есть — пропускаем;
* «сгоревший» (`deleted`) УИП повторно резервируется тем же номером;
* номер формируется локально (`build_local_party_number`);
* по умолчанию (`skip_cz=True`) создаются только ЧЕРНОВИКИ без обращения к ЧЗ —
  чтобы оценить объёмы; при `skip_cz=False` номера резервируются в ЧЗ как
  «свои» пачками не более `CZ_BATCH_SIZE` с паузой `CZ_BATCH_PAUSE_SECONDS`
  между запросами, в разрезе товарных групп;
* генерация номеров самим ЧЗ автоматически не используется;
* при `skip_cz=False`, если заполнение резерва превышает `RELEASE_PERCENT`
  (90%) — накопление пропускается, чтобы не ухудшать ситуацию.
"""

import logging
import time
from datetime import timedelta

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


def _short_shelf_life_days() -> int:
    """Порог срока годности (дней), ниже которого накапливаем резерв."""
    return int(getattr(settings, 'UIP_SHORT_SHELF_LIFE_DAYS', 40) or 40)


def _short_shelf_life_skus():
    """
    Активные SKU обычного формата с коротким сроком годности продукта.
    """
    return (
        ProductSKU.objects.filter(
            is_active=True,
            product__is_active=True,
            product__shelf_life_in_days__lt=_short_shelf_life_days(),
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
        skip_cz: bool = True,
) -> dict:
    """
    Доливает резерв УИП на окно дат для короткоживущей продукции.

    :param pause_seconds: пауза между запросами в ЧЗ (по умолчанию
                          CZ_BATCH_PAUSE_SECONDS). Используется при skip_cz=False.
    :param skip_cz: True (по умолчанию) — создавать только черновики без
                    обращения к ЧЗ (оценка объёмов); False — резервировать
                    «свои» номера в ЧЗ.
    :return: сводка выполнения.
    """
    if pause_seconds is None:
        pause_seconds = CZ_BATCH_PAUSE_SECONDS

    if not skip_cz:
        stats = get_reserve_stats()
        if stats['percent'] > RELEASE_PERCENT:
            message = (
                f'Накопление резерва пропущено: заполнение {stats["percent"]}% '
                f'превышает порог {RELEASE_PERCENT}% '
                f'({stats["count"]}/{stats["limit"]}).'
            )
            logger.warning(message)
            return {
                'is_error': False,
                'skipped': True,
                'skip_cz': False,
                'reason': 'reserve_full',
                'percent': stats['percent'],
                'created': 0,
                'restored': 0,
                'skipped_existing': 0,
                'failed': 0,
                'errors': [],
                'message': message,
            }

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
            'skip_cz': skip_cz,
            'created': 0,
            'restored': 0,
            'skipped_existing': skipped_existing,
            'failed': 0,
            'errors': [],
            'message': message,
        }

    if skip_cz:
        processed = to_process
        errors = []
    else:
        processed, errors = _reserve_numbers(to_process, pause_seconds)

    created, restored = _persist_entries(processed, skip_cz=skip_cz)
    failed = len(to_process) - len(processed)

    mode = 'черновиков' if skip_cz else 'с резервированием в ЧЗ'
    message = (
        f'Накопление резерва УИП ({mode}): создано {created}, '
        f'восстановлено {restored}, пропущено (уже есть) {skipped_existing}, '
        f'ошибок {failed} (из {len(to_process)} к обработке).'
    )
    logger.info(message)
    return {
        'is_error': bool(errors),
        'skipped': False,
        'skip_cz': skip_cz,
        'created': created,
        'restored': restored,
        'skipped_existing': skipped_existing,
        'failed': failed,
        'errors': errors,
        'message': message,
    }
