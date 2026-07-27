# app_cz/services/party_service.py
import json
import logging
import re

import requests
from django.utils import timezone
from django.db.models import Q, ObjectDoesNotExist

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_cz.enums import TypeProduct
from app_cz.services.suz_client import get_true_api_session_token, get_true_api_auth_key
from app_cz.services.code_client import send_application_report
from app_helper.sign_helper import unpinned_signed_data
from app_uip.models import UIP, PartyStatusChoices

logger = logging.getLogger(__name__)


def validate_party_number(party_number: str) -> bool:
    """
    Валидация номера партии (УИП) согласно правилам Честного Знака.
    Формат: 14 цифр (GTIN) + 6 цифр (ГГММДД) + 1-12 символов (a-zA-Z0-9/.,-)
    """
    if not (21 <= len(party_number) <= 32):
        return False

    # Строгая проверка по регулярному выражению
    pattern = r'^\d{14}\d{6}[A-Za-z0-9/.,\-]{1,12}$'
    return bool(re.match(pattern, party_number))


def generate_party_numbers(
        party_info_list: list[dict],
        product_group: str = TypeProduct.MILK.value,
) -> dict:
    """
    Генерируем номера партий со стороны Честного Знака.

    :param party_info_list: Список словарей [{'gtin': '...', 'productionDate': '...', 'count': 1}, ...]
    :param product_group: Товарная группа (например, 'milk', 'bio', 'null')
    :return: Словарь с результатом операции
    """
    if not party_info_list:
        logger.warning("Попытка генерации партий с пустым списком.")
        return {
            'is_error': True,
            'message_error': 'Список параметров для генерации пуст.'
        }

    if len(party_info_list) > 50:
        logger.warning("Попытка сгенерировать более 50 партий за один запрос.")
        return {
            'is_error': True,
            'message_error': 'Максимальное количество генераций за один запрос — 50.'
        }

    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    params = {}

    data = {
        'pg': product_group,
        'partyNumberInfo': party_info_list
    }
    json_dumps_data = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    try:
        logger.info(f"Запрос генерации партий в ЧЗ (группа: {product_group})")

        response = requests.post(
            SUZ.generation_party,
            headers=headers,
            params=params,
            data=json_dumps_data,
            timeout=15
        )

        # Проверка ответа.
        if response.status_code != 200:
            logger.error(f"Ошибка генерации партий {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        if not lst_party_info:
            logger.error(f"Отсутствуют данные о генерации в ответе: {response.text}")
            return {
                'is_error': True,
                'message_error': 'ЧЗ вернул пустой список партий.'
            }

        logger.info("Генерация партий успешно завершена.")
        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при генерации партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def reserve_parties_honest_sign(
        product_group: str,
        party_numbers: list[str]
) -> dict:
    """
    Резервируем свой список номеров партий в Честном Знаке.
    """
    if not party_numbers:
        return {
            'is_error': True,
            'message_error': 'Список партий для резервирования пуст.'
        }

    # 1. Валидация формата
    invalid_parties = [p for p in party_numbers if not validate_party_number(p)]
    if invalid_parties:
        return {
            'is_error': True,
            'message_error': f'Некорректный формат номеров партий: {", ".join(invalid_parties[:3])}...'
        }

    # 2. Проверка на дубликаты в нашей БД (используем новую модель UIP)
    existing_parties = set(
        UIP.objects.filter(
            number__in=party_numbers,
            status__in=[
                PartyStatusChoices.RESERVED_CZ,
                PartyStatusChoices.RESERVED_LOCAL,
                PartyStatusChoices.ACTIVE
            ]
        ).values_list('number', flat=True)
    )

    available_to_reserve = [
        p
        for p in party_numbers
        if p not in existing_parties
    ]

    if not available_to_reserve:
        logger.warning("Все указанные УИП уже зарезервированы или находятся в работе.")
        return {
            'is_error': True,
            'message_error': 'Указанные УИП уже находятся в резерве или в работе.'
        }

    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    params = {}

    data = {
        'pg': product_group,
        'partyNumber': available_to_reserve
    }
    json_dumps_data = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    try:
        logger.info(f"Запрос резервирования {len(available_to_reserve)} партий в ЧЗ.")
        response = requests.post(
            SUZ.reservation_party,
            headers=headers,
            params=params,
            data=json_dumps_data,
            timeout=15
        )

        if response.status_code != 200:
            logger.error(f"Ошибка резервирования {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        logger.info("Резервирование партий успешно завершено.")
        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при резервировании партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def get_all_reserved_parties() -> dict:
    """
    Получает список всех зарезервированных партий со стороны ЧЗ.
    """
    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        logger.info("Запрос списка зарезервированных партий из ЧЗ.")
        response = requests.get(
            SUZ.get_all_reserved_party,
            headers=headers,
            timeout=15
        )

        if response.status_code != 200:
            logger.error(f"Ошибка получения списка {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при получении списка партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def close_party_reservation(
        cis: str,
        batch_number: str,
        is_marking_date: bool = False,
        marking_date: str = None,
        exp_date: str = None,
        exp_date_72: str = None,
) -> dict:
    """
    Снимает партию с резерва путем отправки отчета о нанесении.
    :param cis: Идентификатор продукта.
    :param batch_number: Номер закрываемой партии.
    :param is_marking_date: Флаг необходимости указания даты маркировки.
    :param marking_date: Дата маркировки.
    :param exp_date: Дата окончания срок годности.
    :param exp_date_72: Дата окончания срока годности (срок хранения продукта менее 72 часов).
    """
    # token для работы с TrueAPI.
    try:
        account = SUZAccount.objects.get(is_active=True)
        if not account.dynamic_token:
            raise ValueError("Динамический токен СУЗ отсутствует.")
    except ObjectDoesNotExist:
        return {
            'has_error': True,
            'status_close': False,
            'message': "Активная учётная запись СУЗ не найдена."
        }
    try:
        # 2. Отправляем отчёт о нанесении.
        report_result = send_application_report(
            sntins=[cis],
            batch_number=batch_number,
            is_marking_date=is_marking_date,
            marking_date=marking_date,
            exp_date=exp_date,
            exp_date_72=exp_date_72,
        )

        # 3. Обрабатываем результат.
        if report_result.get('has_error'):
            err_msg = report_result.get(
                'message',
                'Неизвестная ошибка отправки отчёта'
            )
            logger.error(f"Ошибка при закрытии партии {batch_number}: {err_msg}")
            return {
                'has_error': True,
                'status_close': False,
                'message': err_msg
            }

        # 4. Если отчёт отправлен успешно, обновляем статус УИП в нашей БД.
        uip = UIP.objects.filter(number=batch_number).first()
        if uip:
            uip.status = PartyStatusChoices.ACTIVE
            uip.save(update_fields=['status', 'updated_at'])
            logger.info(f"Партия {batch_number} успешно закрыта и переведена в статус ACTIVE.")
        else:
            logger.warning(f"УИП {batch_number} не найден в локальной БД, но отчёт в ЧЗ отправлен успешно.")

        return {
            'has_error': False,
            'status_close': True,
            'message': 'Зарезервированная партия успешно снята с резерва'
        }

    except ValueError as e:
        return {
            'has_error': True,
            'status_close': False,
            'message': str(e)
        }
    except Exception as e:
        logger.exception(f"Критическая ошибка при закрытии партии {batch_number}")
        return {
            'has_error': True,
            'status_close': False,
            'message': f'Внутренняя ошибка: {str}'
        }
