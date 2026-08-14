# app_cz/services/code_client.py

import json
import logging
import requests
from django.core.exceptions import ObjectDoesNotExist

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_cz.enums import TypeProduct, UsedInProduction
from app_helper.sign_helper import unpinned_signed_data

logger = logging.getLogger(__name__)


def send_application_report(
        sntins: list[str],
        is_marking_date: bool = False,
        marking_date: str = None,
        exp_date: str = None,
        exp_date_72: str = None,
        batch_number: str = None,
) -> dict:
    """
    Отправляет отчёт о нанесении кодов в Честный Знак.

    :param sntins: Список кодов DataMatrix (SNTIN).
    :param is_marking_date: Флаг необходимости указания даты маркировки.
    :param marking_date: Дата маркировки.
    :param exp_date: Срок годности (формат ГГГГ-ММ-ДД).
    :param exp_date_72: Срок годности в формате 72 (если применимо).
    :param batch_number: Номер партии.
    :return: Словарь с результатами отправки.
    """
    if not sntins:
        logger.warning("Попытка отправить пустой отчёт о нанесении.")
        return {
            'has_error': False,
            'status_close': True,
            'responses': []
        }

    # 1. Валидация входных данных.
    # if is_marking_date and not marking_date:
    #     raise ValueError('Флаг is_marking_date установлен, но marking_date не указана')
    #
    # if not exp_date and not exp_date_72:
    #     raise ValueError('Необходимо указать срок годности (exp_date или exp_date_72)')

    # 2. Получение учётных данных.
    try:
        account = SUZAccount.objects.get(is_active=True)
    except ObjectDoesNotExist:
        raise ValueError("Активная учётная запись СУЗ не найдена в системе")

    client_token = account.dynamic_token
    oms_id = account.oms_id

    if not client_token:
        raise ValueError("Динамический токен СУЗ отсутствует. Обновите его перед отправкой отчёта.")

    url = SUZ.utilisation
    type_product = TypeProduct.MILK.value
    used_in_production = UsedInProduction.not_were_used_to_produce.value

    report_list = []
    responses = []

    if exp_date and exp_date_72:
        raise ValueError(
            "Некорректные данные о сроке годности продукта: "
            "указан обычный и для скоропортящегося."
        )

    # if (
    #         marking_date is None
    #         and exp_date is None
    #         and exp_date_72 is None
    # ):
    #     raise ValueError("Отсутствуют данные о сроках продукта.")


    # 3. Формирование чанков (максимум 30 000 кодов на один запрос).
    chunk_size = 30000
    for i in range(0, len(sntins), chunk_size):
        chunk = sntins[i:i + chunk_size]

        attributes = {
            "usedInProduction": used_in_production,
        }

        if exp_date_72 and not exp_date:
            attributes["expDate72"] = exp_date_72

        if exp_date and not exp_date_72:
            attributes["expDate"] = exp_date

        if is_marking_date and marking_date:
            attributes["productionDate"] = marking_date

        if batch_number:
            attributes["batchNumber"] = batch_number

        payload = {
            "productGroup": type_product,
            "sntins": chunk,
            "attributes": attributes
        }

        # Каноникализация JSON (без пробелов) для корректной подписи.
        json_data = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        # Подпись данных
        _, signed_data = unpinned_signed_data(json_data)

        report_list.append({
            'row_data': json_data,
            'signed_data': signed_data
        })

    # 4. Отправка запросов.
    base_headers = {
        "clientToken": client_token,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    params = {"omsId": oms_id}

    for idx, report in enumerate(report_list):
        # Создаём копию заголовков для каждого запроса, чтобы безопасно добавить подпись.
        headers = base_headers.copy()
        headers["X-Signature"] = report['signed_data']

        try:
            logger.info(
                f"Отправка отчёта о нанесении (чанк {idx + 1}/{len(report_list)}, кодов: {len(sntins)})"
            )

            response = requests.post(
                url=url,
                params=params,
                headers=headers,
                data=report['row_data'],
                timeout=30
            )

            # Пытаемся распарсить JSON, даже если статус не 200 (ЧЗ часто кладёт описание ошибки в тело).
            try:
                resp_json = response.json()
            except ValueError:
                resp_json = {"raw_response": response.text}

            responses.append(resp_json)

            # Проверка HTTP статуса
            if response.status_code != 200:
                error_msg = resp_json.get('errorMessage') or resp_json.get('error_message') or response.text
                logger.error(f"Ошибка HTTP {response.status_code} при отправке отчёта: {error_msg}")
                return {
                    'has_error': True,
                    'status_close': False,
                    'message': f'Ошибка ЧЗ ({response.status_code}): {error_msg}',
                    'responses': responses
                }

            # Проверка логических ошибок внутри ответа 200 OK (особенность API ЧЗ).
            if resp_json.get('globalErrors') or resp_json.get('hasErrors'):
                logger.error(f"Логическая ошибка в отчёте нанесения: {resp_json}")
                return {
                    'has_error': True,
                    'status_close': False,
                    'message': f'Ошибка в данных отчёта: {resp_json.get("globalErrors") or resp_json}',
                    'responses': responses
                }

        except requests.exceptions.RequestException as e:
            logger.exception("Критическая ошибка сети при отправке отчёта о нанесении")
            return {
                'has_error': True,
                'status_close': False,
                'message': f'Ошибка сети: {str(e)}',
                'responses': responses
            }

    logger.info(f"Отчёт о нанесении успешно отправлен. Обработано кодов: {len(sntins)}")
    return {
        'has_error': False,
        'status_close': True,
        'responses': responses
    }
