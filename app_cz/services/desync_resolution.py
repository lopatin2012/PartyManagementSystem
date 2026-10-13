# app_cz/services/desync_resolution.py

"""
Автоматический разбор УИП в рассинхроне (`UIP.is_desync=True`) по кодам.

Рассинхрон копится в двух видах, и задача разбирает оба:

* `reserved_cz` / `reserved_local`, которых больше нет в резерве ЧЗ: если по
  коду из внешнего задания отчёт о нанесении сформирован (код APPLIED/INTRODUCED
  в ЧЗ) — УИП переводится в `registered`; дальше работает обычная статусная
  лестница (`closed` → `archived`).
* `registered` / `closed` / `archived`, которые всё ещё числятся в резерве ЧЗ:
  если код нанесён, регистрация подтверждена — снимается только флаг
  `is_desync`, статус не меняется.

Код для проверки берётся из локальных `CISCode` УИП; если локальных кодов нет —
запрашивается один код у сервера завода (`get_any_code_for_task` по
`external_number_task`), как в `register_uip`. Статусы кодов проверяются
пакетно в ЧЗ (`POST /cises/info`, до 1000 кодов за запрос, по товарной группе),
поэтому одиночных обращений не делаем.

Если отчёт не сформирован (код не APPLIED) или кода нет — УИП остаётся в
рассинхроне и разбирается администратором вручную.
"""

import logging

from app_cz.models import CISCode, CISCodesStatusChoices
from app_uip.models import UIP, PartyStatusChoices, ProductionParty

logger = logging.getLogger(__name__)

# Максимум УИП, разбираемых за один прогон (защита от большой очереди).
MAX_UIPS_PER_RUN = 200
# Максимум кодов одного УИП, отправляемых в /cises/info за прогон.
MAX_CODES_PER_UIP = 100
# Максимум внешних запросов кода у серверов заводов за один прогон
# (get_any_code_for_task делает до одного сетевого запроса на УИП без
# локальных кодов; ограничение не даёт недоступному заводу затормозить воркер).
MAX_EXTERNAL_FETCHES = 30

# Статусы, которые переводятся в REGISTERED при подтверждении регистрации.
RESERVED_STATUSES = [
    PartyStatusChoices.RESERVED_CZ,
    PartyStatusChoices.RESERVED_LOCAL,
]

# Статусы, для которых достаточно снять флаг рассинхрона.
REGISTERED_STATUSES = [
    PartyStatusChoices.REGISTERED,
    PartyStatusChoices.CLOSED,
    PartyStatusChoices.ARCHIVED,
]

# Статусы кода в ЧЗ, означающие, что код нанесён (отчёт сформирован).
APPLIED_STATUSES = {
    CISCodesStatusChoices.APPLIED,
    CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION,
}

# Статусы, которые задача не трогает.
SKIPPED_STATUSES = [PartyStatusChoices.DRAFT, PartyStatusChoices.DELETED]

REGISTERED_NOTE = (
    'Разбор рассинхрона: код нанесён в ЧЗ (отчёт о нанесении сформирован)'
)


def _local_codes(uip: UIP) -> list:
    """До MAX_CODES_PER_UIP локальных кодов УИП (старейшие первыми)."""
    return list(
        CISCode.objects
        .filter(production_party__uip=uip)
        .order_by('id')
        .values_list('code', flat=True)[:MAX_CODES_PER_UIP]
    )


def _fetch_external_code(uip: UIP) -> str:
    """
    Запрашивает один код у сервера завода (get_any_code_for_task).

    Используется, когда у УИП нет локальных кодов: код тянется из последнего
    внешнего задания. При недоступности сервера/ошибке возвращает None.
    """
    party = (
        ProductionParty.objects
        .filter(uip=uip)
        .exclude(external_number_task='')
        .order_by('-production_datetime_start', '-created_at')
        .first()
    )
    if not party or not party.external_number_task:
        return None

    from app_cz.services.reserve_monitor import _fetch_code_for_task

    return _fetch_code_for_task(uip, party.external_number_task)


def _collect_codes(uips: list, fetch_external: bool) -> dict:
    """
    Собирает коды по УИП: локальные, при отсутствии — из внешнего задания.

    Число внешних запросов ограничено MAX_EXTERNAL_FETCHES за прогон: остальные
    УИП без кодов будут проверены в следующих прогонах.
    """
    codes_by_uip = {}
    external_fetches = 0
    for uip in uips:
        codes = _local_codes(uip)
        if (
            not codes
            and fetch_external
            and external_fetches < MAX_EXTERNAL_FETCHES
        ):
            external_fetches += 1
            external = _fetch_external_code(uip)
            if external:
                codes = [external]
        if codes:
            codes_by_uip[uip.id] = codes
    return codes_by_uip


def _applied_codes(codes_by_uip: dict, uip_by_id: dict, token: str = None) -> set:
    """
    Возвращает множество кодов УИП, нанесённых (APPLIED) в ЧЗ.

    Коды группируются по товарной группе (`pg`) и проверяются пакетно.
    """
    from app_cz.services.code_status import get_cises_statuses, map_cz_status

    codes_by_group = {}
    for uip_id, codes in codes_by_uip.items():
        uip = uip_by_id.get(uip_id)
        if not uip or not uip.product_sku_id:
            continue
        group = uip.product_sku.product.group
        if not group:
            continue
        codes_by_group.setdefault(group, []).extend(codes)

    applied = set()
    for group, codes in codes_by_group.items():
        for code, raw_status in get_cises_statuses(codes, group, token=token).items():
            if map_cz_status(raw_status) in APPLIED_STATUSES:
                applied.add(code)
    return applied


def _register_by_code(uip: UIP, source: str, note: str) -> tuple:
    """
    Код(ы) УИП нанесены в ЧЗ → подтверждаем регистрацию.

    `reserved_*` переводится в `registered`; `registered`/`closed`/`archived`
    статус не меняют (только снимается `is_desync`); `draft`/`deleted` не
    трогаются.

    :return: (status_changed, desync_cleared)
    """
    status_changed = False
    if uip.status in RESERVED_STATUSES:
        uip.change_status(
            PartyStatusChoices.REGISTERED, source=source, note=note,
        )
        status_changed = True
    elif uip.status not in REGISTERED_STATUSES:
        return False, False

    desync_cleared = False
    if uip.is_desync:
        uip.is_desync = False
        uip.save(update_fields=['is_desync', 'updated_at'])
        desync_cleared = True
    return status_changed, desync_cleared


def resolve_desync_uips(fetch_external: bool = True, max_uips: int = MAX_UIPS_PER_RUN) -> dict:
    """
    Разбирает УИП в рассинхроне (`is_desync=True`) по кодам из внешних заданий.

    :param fetch_external: если локальных кодов нет — запрашивать код у сервера
                           завода (get_any_code_for_task).
    :param max_uips: максимум УИП за один прогон.
    :return: сводка по разбору.
    """
    summary = {
        'is_error': False,
        'checked': 0,
        'registered': 0,
        'desync_cleared': 0,
        'remaining': 0,
        'message': '',
    }

    uips = list(
        UIP.objects
        .filter(is_desync=True)
        .exclude(status__in=SKIPPED_STATUSES)
        .select_related('product_sku__product')
        .order_by('updated_at')[:max_uips]
    )

    if not uips:
        summary['message'] = 'Нет УИП в рассинхроне'
        logger.info(summary['message'])
        return summary

    uip_by_id = {uip.id: uip for uip in uips}
    codes_by_uip = _collect_codes(uips, fetch_external)
    applied = _applied_codes(codes_by_uip, uip_by_id)

    for uip in uips:
        summary['checked'] += 1

        codes = codes_by_uip.get(uip.id)
        if not codes or not any(code in applied for code in codes):
            # Отчёт не сформирован (или кода нет) — оставляем рассинхрон.
            summary['remaining'] += 1
            continue

        # Код нанесён в ЧЗ → отчёт о нанесении сформирован.
        try:
            status_changed, desync_cleared = _register_by_code(
                uip, source='sync', note=REGISTERED_NOTE,
            )
            if not status_changed and not desync_cleared:
                # Неожиданный статус — не трогаем, оставляем администратору.
                summary['remaining'] += 1
                continue
            summary['registered'] += int(status_changed)
            summary['desync_cleared'] += int(desync_cleared)
        except Exception:
            logger.exception(
                f'Разбор рассинхрона: ошибка обновления УИП {uip.number}'
            )
            summary['is_error'] = True
            summary['remaining'] += 1

    summary['message'] = (
        f'Разбор рассинхрона: проверено {summary["checked"]}, '
        f'зарегистрировано {summary["registered"]}, '
        f'снят флаг у {summary["desync_cleared"]}, '
        f'осталось {summary["remaining"]}'
    )
    logger.info(summary['message'])
    return summary


def resolve_desync_uip(
        uip: UIP,
        source: str = 'sync',
        note: str = None,
        fetch_external: bool = True,
) -> bool:
    """
    Разбирает один УИП по кодам из его заданий (ручная проверка в ЧЗ).

    Если код нанесён в ЧЗ (APPLIED/INTRODUCED) — `reserved_*` переводится в
    `registered`, а у `registered`/`closed`/`archived` снимается `is_desync`.

    :return: True, если регистрация подтверждена кодом.
    """
    codes_by_uip = _collect_codes([uip], fetch_external)
    applied = _applied_codes(codes_by_uip, {uip.id: uip})
    codes = codes_by_uip.get(uip.id) or []
    if not codes or not any(code in applied for code in codes):
        return False

    _register_by_code(uip, source=source, note=note or REGISTERED_NOTE)
    return True


def register_desync_uip(uip: UIP, source: str = 'sync', note: str = None) -> tuple:
    """
    Подтверждает регистрацию УИП без проверки кодов: `reserved_*` → `registered`,
    у `registered`/`closed`/`archived` снимается `is_desync`.

    Используется, когда регистрация следует из других признаков (например,
    номера нет в резерве ЧЗ и зарезервировать его не удалось).

    :return: (status_changed, desync_cleared)
    """
    return _register_by_code(uip, source=source, note=note or REGISTERED_NOTE)
