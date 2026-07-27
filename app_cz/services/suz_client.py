# app_cz/services/suz_client.py

import logging
from datetime import timedelta
import uuid

import requests
from django.utils import timezone

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ

from app_helper.sign_helper import attached_signed_data, unpinned_signed_data

logger = logging.getLogger(__name__)


def get_true_api_auth_key() -> dict:
    """
    Получает uuid и data для последующей подписи и получения токена.
    :return: Словарь {'uuid': '...', 'data': '...'}
    :raises Exception: Если запрос к TrueAPI не удался
    """
    try:
        response = requests.get(SUZ.auth_key, timeout=10)

        response.raise_for_status()

        return response.json()

    except requests.exceptions.Timeout:
        logger.error("Превышено время ожидания ответа от TrueAPI (auth_key)")
        raise Exception("Сервис Честного Знака не отвечает. Попробуйте позже.")

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP ошибка при запросе auth_key: {e.response.status_code} - {e.response.text}")
        raise Exception(f"Ошибка сервера Честного Знака: {e.response.status_code}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при запросе auth_key: {e}")
        raise Exception("Не удалось соединиться с сервисом Честного Знака.")

    except ValueError:
        logger.error("Некорректный JSON в ответе от TrueAPI")
        raise Exception("Сервер Честного Знака вернул некорректные данные.")


def get_true_api_session_token() -> dict:
    """
    Получает базовый токен сессии TrueAPI.
    Может потребоваться для некоторых специфичных методов API.
    """
    try:
        # 1. Получаем ключи для подписи.
        auth_data = get_true_api_auth_key()
        row_uuid = auth_data['uuid']
        row_data = auth_data['data']

        # 2. Подписываем данные.
        _, signed_data = attached_signed_data(row_data)

        # 3. Берём активный аккаунт для получения ИНН.
        account = SUZAccount.objects.filter(is_active=True).first()
        if not account:
            raise ValueError("Активная учётная запись СУЗ не найдена")

        # 4. Формируем запрос.
        url = SUZ.simple_sign_in
        payload = {
            'uuid': row_uuid,
            'data': signed_data,
            'inn': account.inn,
            'unitedToken': True
        }

        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15
        )
        response.raise_for_status()

        result = response.json()
        token = result.get('token') or result.get('uuidToken')

        if not token:
            raise ValueError("Токен отсутствует в ответе сервера Честного Знака")

        logger.info("Базовый токен сессии TrueAPI успешно получен")
        return {'uuid': row_uuid, 'token': token}

    except Exception as e:
        logger.error(f"Ошибка получения базового токена TrueAPI: {e}")
        raise


def get_true_api_dynamic_token(row_uuid: str, signed_data: str) -> str:
    """
    Получает динамический токен для работы с СУЗ (unitedToken=True).
    """
    try:
        account = SUZAccount.objects.filter(is_active=True).first()
        if not account:
            raise ValueError("Активная учётная запись СУЗ не найдена")

        # Формируем URL с идентификатором соединения
        url = f"{SUZ.simple_sign_in}/{account.connection_identifier}"

        payload = {
            'uuid': row_uuid,
            'data': signed_data,
            'inn': account.inn,
            'unitedToken': True
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "omsConnection": account.connection_identifier
        }

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=15
        )

        response.raise_for_status()

        result = response.json()
        token = result.get('token')

        if not token:
            raise ValueError("Динамический токен отсутствует в ответе сервера")

        logger.info("Динамический токен TrueAPI успешно получен")
        return token

    except Exception as e:
        logger.error(f"Ошибка получения динамического токена TrueAPI: {e}")
        raise


def refresh_suz_dynamic_token() -> bool:
    """
    Полный цикл обновления динамического токена СУЗ.
    Возвращает True при успехе, False при неудаче.
    """
    try:
        # 1. Получаем ключи для подписи.
        auth_data = get_true_api_auth_key()
        row_uuid = auth_data['uuid']
        row_data = auth_data['data']

        # 2. Подписываем данные (прикреплённая подпись).
        _, signed_data = attached_signed_data(row_data)

        # 3. Получаем динамический токен.
        dynamic_token = get_true_api_dynamic_token(row_uuid, signed_data)

        # Проверка корректности токена.
        try:
            uuid.UUID(dynamic_token)
        except ValueError:
            logger.error(
                f"Получен некорректный UUID: {dynamic_token}")
            return False

        # 5. Сохраняем в БД.
        account = SUZAccount.objects.filter(is_active=True).first()
        if not account:
            logger.error("Активная учётная запись СУЗ не найдена перед сохранением")
            return False

        account.dynamic_token = dynamic_token
        # ЧЗ обычно выдаёт токен на 10 часов. Сохраняем с небольшим запасом на 8 часов.
        account.token_expires_at = timezone.now() + timedelta(hours=8)

        account.save(update_fields=['dynamic_token', 'token_expires_at', 'updated_at'])

        logger.info(f"Динамический токен успешно обновлён и сохранён для {account.certificate_name}")
        return True

    except Exception as e:
        logger.error(f"Критическая ошибка при обновлении токена СУЗ: {e}")
        return False
