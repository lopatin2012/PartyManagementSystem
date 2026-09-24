# app_cz/services/code_status.py

"""
Пакетная проверка статусов кодов маркировки в Честном Знаке.

Локальный `CISCode.cz_status` (со стороны завода/1С) не обновляется после
обработки отчётов о нанесении, поэтому фактический статус кода берётся
напрямую из ЧЗ методом:

    POST /api/v3/true-api/cises/info?pg=<товарная группа>
    Body: ["<cis>", "<cis>", ...]   (до 1000 кодов за запрос)
    Ответ: [{"cisInfo": {"cis": "...", "status": "APPLIED"|"INTRODUCED"|...}}, ...]

Метод вызывается ОДИН раз на пачку кодов, чтобы не создавать поток
одиночных запросов (ЧЗ может расценить это как спам и заблокировать обработку).
"""

import logging

import requests
from django.core.exceptions import ObjectDoesNotExist

from app_cz.models import CISCodesStatusChoices, SUZAccount
from app_cz.suz_config import SUZ
from app_cz.services.suz_client import get_true_api_session_token

logger = logging.getLogger(__name__)

# Максимум кодов в одном запросе к /cises/info (ограничение ЧЗ).
CISES_INFO_BATCH_SIZE = 1000

# Статусы ЧЗ → внутренние значения cz_status.
CZ_STATUS_MAP = {
    'EMITTED': CISCodesStatusChoices.EMITTED,
    'APPLIED': CISCodesStatusChoices.APPLIED,
    'INTRODUCED': CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION,
    'INTRODUCED_INTO_CIRCULATION': CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION,
    'WRITTEN_OFF': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    'RETIRED': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    'WITHDRAWN': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
}


def map_cz_status(raw_status: str) -> int:
    """Строковый статус ЧЗ → значение CISCodesStatusChoices."""
    if not raw_status:
        return CISCodesStatusChoices.EMITTED
    return CZ_STATUS_MAP.get(str(raw_status).strip().upper(), CISCodesStatusChoices.EMITTED)


def _extract_statuses(payload) -> dict:
    """Разбирает ответ /cises/info в {code: raw_status}."""
    result = {}
    if not isinstance(payload, list):
        return result
    for item in payload:
        if not isinstance(item, dict):
            continue
        info = item.get('cisInfo') or item.get('cis_info') or item
        if not isinstance(info, dict):
            continue
        code = info.get('cis') or info.get('requestedCis')
        if not code:
            continue
        result[str(code).strip()] = info.get('status')
    return result


def get_cises_statuses(codes: list, product_group: str) -> dict:
    """
    Пакетно запрашивает статусы кодов в ЧЗ.

    :param codes: список кодов DataMatrix (CIS).
    :param product_group: товарная группа ЧЗ (например, 'milk').
    :return: {code: raw_status} — только найденные коды. Пустой dict при ошибке.
    """
    codes = [str(c).strip() for c in codes if c]
    if not codes:
        return {}

    try:
        account = SUZAccount.objects.get(is_active=True)
    except ObjectDoesNotExist:
        logger.warning('Проверка статусов кодов: активная учётная запись СУЗ не найдена.')
        return {}

    if not account.dynamic_token:
        logger.warning('Проверка статусов кодов: отсутствует динамический токен СУЗ.')
        return {}

    statuses = {}
    for start in range(0, len(codes), CISES_INFO_BATCH_SIZE):
        chunk = codes[start:start + CISES_INFO_BATCH_SIZE]
        try:
            response = requests.post(
                SUZ.cises_info,
                params={'pg': product_group},
                headers={
                    'clientToken': account.dynamic_token,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                json=chunk,
                timeout=30,
            )
            response.raise_for_status()
            statuses.update(_extract_statuses(response.json()))
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(
                f'Ошибка пакетной проверки статусов кодов в ЧЗ '
                f'(кодов: {len(chunk)}): {e}'
            )
            continue

    return statuses
