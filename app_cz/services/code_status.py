# app_cz/services/code_status.py

"""
Пакетная проверка статусов кодов маркировки в Честном Знаке.

Локальный `CISCode.cz_status` (со стороны завода/1С) не обновляется после
обработки отчётов о нанесении, поэтому фактический статус кода берётся
напрямую из ЧЗ методом:

    POST /api/v3/true-api/cises/info?pg=<товарная группа>
    Body: ["<cis>", "<cis>", ...]   (до 1000 кодов за запрос)
    Ответ: [{"cisInfo": {"cis": "...", "status": "APPLIED"|"INTRODUCED"|...}}, ...]

Авторизация — как у остальных методов True API (markirovka.crpt.ru):
`Authorization: Bearer <токен сессии из /auth/simpleSignIn>`. Заголовок
`clientToken` относится к СУЗ (suzgrid.crpt.ru) и здесь даёт 401.

Коды передаются в нормализованном виде (без управляющего разделителя GS и
AI 93), как в «Молвест.Маркировка» (`check_status_codes_in_honest_sign`):
для молочной группы это первые 24 символа КИ (01 + GTIN(14) + 21 + серийный
номер(6)).

Метод вызывается ОДИН раз на пачку кодов, чтобы не создавать поток
одиночных запросов (ЧЗ может расценить это как спам и заблокировать обработку).
"""

import logging
import time

import requests

from app_cz.models import CISCodesStatusChoices
from app_cz.suz_config import SUZ

logger = logging.getLogger(__name__)

# Максимум кодов в одном запросе к /cises/info (ограничение ЧЗ).
CISES_INFO_BATCH_SIZE = 1000

# Пауза между запросами (как в «Молвест.Маркировка») — защита от блокировки
# за спам со стороны ЧЗ.
CISES_INFO_PAUSE_SECONDS = 1

# Длина нормализованного КИ (CIS) по товарной группе. Для молочной продукции —
# 24 (01 + GTIN(14) + 21 + серийный номер(6)); всё, что дальше (GS, AI 93),
# в /cises/info не передаётся.
CIS_LENGTH_BY_GROUP = {
    'milk': 24,
}

# Статусы ЧЗ → внутренние значения cz_status.
CZ_STATUS_MAP = {
    'EMITTED': CISCodesStatusChoices.EMITTED,
    'APPLIED': CISCodesStatusChoices.APPLIED,
    'INTRODUCED': CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION,
    'INTRODUCED_INTO_CIRCULATION': CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION,
    'WRITTEN_OFF': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    'RETIRED': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    'WITHDRAWN': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    # Расформирован (агрегат/КиЗ) — код больше не в обороте.
    'DISAGGREGATION': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
    'DISAGGREGATED': CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION,
}


def map_cz_status(raw_status: str) -> int:
    """Строковый статус ЧЗ → значение CISCodesStatusChoices."""
    if not raw_status:
        return CISCodesStatusChoices.EMITTED
    return CZ_STATUS_MAP.get(str(raw_status).strip().upper(), CISCodesStatusChoices.EMITTED)


def normalize_cis(code: str, product_group: str) -> str:
    """
    Нормализует код до КИ (CIS) для запроса в /cises/info.

    Убирает управляющие символы (GS/FNC1) и обрезает до длины КИ товарной
    группы (для молочной — 24), отрезая AI 93 и прочую агрегацию.
    """
    cleaned = ''.join(ch for ch in str(code) if ord(ch) >= 32)
    length = CIS_LENGTH_BY_GROUP.get(product_group)
    return cleaned[:length] if length else cleaned


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
        # Предпочитаем requestedCis — это код, который мы сами отправили
        # (нормализованный), чтобы вернуть статус по исходному коду.
        code = info.get('requestedCis') or info.get('cis')
        if not code:
            continue
        result[str(code).strip()] = info.get('status')
    return result


def get_cises_statuses(codes: list, product_group: str, token: str = None) -> dict:
    """
    Пакетно запрашивает статусы кодов в ЧЗ.

    :param codes: список кодов DataMatrix (CIS).
    :param product_group: товарная группа ЧЗ (например, 'milk').
    :param token: готовый токен True API (если не передан — получаем сами).
    :return: {code: raw_status} — только найденные коды. Пустой dict при ошибке.
    """
    # Нормализуем коды и запоминаем соответствие «нормализованный → исходный»,
    # чтобы вернуть статус по тому коду, который передал вызывающий.
    normalized_to_original = {}
    normalized = []
    for raw_code in codes:
        original = str(raw_code).strip()
        if not original:
            continue
        cis = normalize_cis(original, product_group)
        if not cis:
            continue
        normalized_to_original.setdefault(cis, original)
        normalized.append(cis)

    normalized = list(dict.fromkeys(normalized))
    if not normalized:
        return {}

    if not token:
        from app_cz.services.suz_client import get_true_api_session_token
        token = (get_true_api_session_token() or {}).get('token')
    if not token:
        logger.warning('Проверка статусов кодов: не удалось получить токен True API.')
        return {}

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    statuses = {}
    for start in range(0, len(normalized), CISES_INFO_BATCH_SIZE):
        chunk = normalized[start:start + CISES_INFO_BATCH_SIZE]
        time.sleep(CISES_INFO_PAUSE_SECONDS)
        try:
            response = requests.post(
                SUZ.cises_info,
                params={'pg': product_group},
                headers=headers,
                json=chunk,
                timeout=30,
            )
            response.raise_for_status()
            found = _extract_statuses(response.json())
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(
                f'Ошибка пакетной проверки статусов кодов в ЧЗ '
                f'(кодов: {len(chunk)}): {e}'
            )
            continue

        for code, raw_status in found.items():
            statuses[normalized_to_original.get(code, code)] = raw_status

    return statuses
