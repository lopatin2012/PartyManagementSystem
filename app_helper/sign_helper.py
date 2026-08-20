# app_helper/sign_helper.py

import logging
import json

import requests
from django.core.exceptions import ObjectDoesNotExist

from app_cz.models import SUZAccount
from config.models import ExternalService, TypeServiceChoices

logger = logging.getLogger(__name__)

# Таймаут запросов к сервису подписей, сек.
SIGN_TIMEOUT = 30


def _get_signature_service_url() -> str:
    """Возвращает базовый URL внешнего сервиса подписей из таблицы ExternalService."""
    try:
        service = ExternalService.objects.get(
            service_type=TypeServiceChoices.SIGNATURE,
            is_active=True,
        )
    except ObjectDoesNotExist:
        raise RuntimeError(
            'Внешний сервис подписей не настроен в админке '
            '(раздел "Внешние сервисы", тип "Сервис подписей").'
        )

    return f'http://{service.ip_address}:{service.port_address}'


def _get_active_serial_number() -> str:
    """Возвращает серийный номер активной учётной записи СУЗ."""
    try:
        account = SUZAccount.objects.get(is_active=True)
    except SUZAccount.DoesNotExist:
        raise ValueError('Активная учётная запись СУЗ не найдена')

    return account.serial_number


def _post_sign(endpoint: str, data: str, serial_number: str) -> str:
    """Отправляет данные на подпись во внешний сервис и возвращает подпись."""
    base_url = _get_signature_service_url()
    payload = {
        'data': data,
        'serial_number': serial_number,
    }

    try:
        response = requests.post(
            f'{base_url}{endpoint}',
            json=payload,
            timeout=SIGN_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f'Сетевая ошибка при обращении к сервису подписей ({base_url}{endpoint}): {e}')
        raise RuntimeError('Не удалось соединиться с внешним сервисом подписей.')

    if response.status_code == 404:
        detail = response.json().get('detail', 'Сертификат не найден')
        raise ValueError(detail)

    if response.status_code != 200:
        logger.error(
            f'Ошибка сервиса подписей ({base_url}{endpoint}): '
            f'{response.status_code} - {response.text}'
        )
        raise RuntimeError(f'Ошибка внешнего сервиса подписей: {response.status_code}')

    result = response.json()
    signed_data = result.get('signed_data')
    if not signed_data:
        raise RuntimeError('Внешний сервис подписей не вернул подпись.')

    return signed_data


def get_list_certificates() -> list:
    """Получает список валидных сертификатов из внешнего сервиса подписей."""
    base_url = _get_signature_service_url()

    try:
        response = requests.get(f'{base_url}/api/certificates/', timeout=SIGN_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.error(f'Сетевая ошибка при обращении к сервису подписей ({base_url}/api/certificates/): {e}')
        return []

    if response.status_code != 200:
        logger.error(f'Ошибка сервиса подписей: {response.status_code} - {response.text}')
        return []

    data = response.json()
    certificates = data.get('certificates', [])
    logger.info(f"Успешно найдено {len(certificates)} валидных сертификатов.")
    return certificates


def attached_signed_data(row_data: str) -> tuple[str, str]:
    """
    Создаёт прикреплённую подпись через внешний сервис подписей.
    :param row_data: Данные для подписи.
    :return: (row_data, signed_data)
    """
    serial_number = _get_active_serial_number()
    signed_data = _post_sign('/api/sign/attached/', row_data, serial_number)
    return row_data, signed_data


def unpinned_signed_data(row_data) -> tuple[str, str]:
    """
    Создаёт откреплённую подпись через внешний сервис подписей.
    :param row_data: Данные для подписи (строка или dict/list).
    :return: (row_data, signed_data)
    """
    # Канонизация данных в строку (без пробелов) для корректной подписи.
    if isinstance(row_data, (dict, list)):
        message = json.dumps(row_data, separators=(',', ':'), ensure_ascii=False)
    else:
        message = str(row_data)

    serial_number = _get_active_serial_number()
    signed_data = _post_sign('/api/sign/unpinned/', message, serial_number)
    return row_data, signed_data