# app_helper/sign_helper.py

import logging
import json
import os
from datetime import datetime

import requests
from django.core.exceptions import ObjectDoesNotExist

from app_cz.models import SUZAccount
from config.models import ExternalService, TypeServiceChoices

logger = logging.getLogger(__name__)

# Таймаут запросов к сервису подписей, сек.
SIGN_TIMEOUT = 30

# Прямой адрес сервиса подписей (переопределяет запись в БД ExternalService).
# По умолчанию — адрес, опубликованный на хосте (см. docker-compose).
SIGNATURE_SERVICE_URL = os.getenv('SIGNATURE_SERVICE_URL', 'http://127.0.0.1:8001')

# В выпадающем списке показывать только один сертификат (по серийному номеру).
# Если переменная пустая/не задана — список не фильтруется.
ALLOWED_CERTIFICATE_SERIAL = os.getenv(
    'SUZ_ALLOWED_CERTIFICATE_SERIAL', '3F11640082B43D8544A2D8787B3ED255'
)


def _get_signature_service_url() -> str:
    """Возвращает базовый URL внешнего сервиса подписей.

    Если задана переменная окружения SIGNATURE_SERVICE_URL — она имеет приоритет
    (например, внутри docker-compose указывается адрес контейнера signature-service).
    Иначе адрес берётся из таблицы ExternalService.
    """
    if SIGNATURE_SERVICE_URL:
        return SIGNATURE_SERVICE_URL.rstrip('/')

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

    # Приведение полей сервиса подписей к ожидаемым на фронте и в views:
    # subject -> fio, valid_to -> valid_for + расчёт оставшихся дней valid_days.
    normalized = []
    for cert in certificates:
        valid_for = str(cert.get('valid_to', ''))
        valid_days = 0
        try:
            valid_days = (datetime.strptime(valid_for, "%d-%m-%Y") - datetime.now()).days
        except (ValueError, TypeError):
            valid_days = 0
        normalized.append({
            'serial_number': cert.get('serial_number'),
            'fio': cert.get('subject', ''),
            'valid_for': valid_for,
            'valid_days': max(valid_days, 0),
            'valid_from': cert.get('valid_from'),
        })

    logger.info(f"Успешно найдено {len(normalized)} валидных сертификатов.")

    # Ограничиваем выпадающий список одним сертификатом (серийный номер из env).
    if ALLOWED_CERTIFICATE_SERIAL:
        wanted = ALLOWED_CERTIFICATE_SERIAL.upper()
        filtered = [
            cert for cert in normalized
            if str(cert.get('serial_number', '')).upper() == wanted
        ]
        if filtered:
            logger.info(f"Отфильтровано: показан только сертификат с серийным номером {wanted}.")
            return filtered
        logger.warning(f"Сертификат с серийным номером {wanted} не найден в хранилище — список пуст.")

    return normalized


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