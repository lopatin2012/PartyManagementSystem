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
    Получает базовый токен сессии TrueAPI (unitedToken).

    Контракт (как в «Молвест.Маркировка»): функция НИКОГДА не бросает
    исключение — при любой ошибке возвращает
    ``{'token': None, 'message': <причина>}``, чтобы вызывающий код
    показывал понятную ошибку, а не падал с 500.

    :return: {'uuid': str|None, 'token': str|None, 'message': str}
    """
    # 1. Ключ для подписи (GET /auth/key).
    try:
        auth_data = get_true_api_auth_key()
        row_uuid = auth_data['uuid']
        row_data = auth_data['data']
    except Exception as e:
        logger.error(f"Не удалось получить auth_key TrueAPI: {e}")
        return {'uuid': None, 'token': None, 'message': str(e)}

    # 2. Подпись данных (прикреплённая).
    try:
        _, signed_data = attached_signed_data(row_data)
    except Exception as e:
        logger.error(f"Не удалось подписать данные для TrueAPI: {e}")
        return {'uuid': row_uuid, 'token': None, 'message': str(e)}

    if not signed_data:
        return {
            'uuid': row_uuid,
            'token': None,
            'message': 'Сервис подписей не вернул подпись. Проверьте сертификат.',
        }

    # 3. Активная учётная запись (ИНН).
    account = SUZAccount.objects.filter(is_active=True).first()
    if not account:
        return {
            'uuid': row_uuid,
            'token': None,
            'message': 'Активная учётная запись СУЗ не найдена.',
        }

    # 4. simpleSignIn → токен. При unitedToken ЧЗ возвращает `uuidToken`
    #    (как в «Молвест.Маркировка»), поэтому читаем его ПЕРВЫМ.
    url = SUZ.simple_sign_in
    payload = {
        'uuid': row_uuid,
        'data': signed_data,
        'inn': account.inn,
        'unitedToken': True,
    }
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при получении токена TrueAPI: {e}")
        return {
            'uuid': row_uuid,
            'token': None,
            'message': f'Ошибка соединения с TrueAPI: {e}',
        }

    if response.status_code != 200:
        detail = _extract_true_api_error(response)
        logger.error(
            f"TrueAPI simpleSignIn вернул {response.status_code}: {detail}"
        )
        return {'uuid': row_uuid, 'token': None, 'message': detail}

    try:
        result = response.json()
    except ValueError:
        return {
            'uuid': row_uuid,
            'token': None,
            'message': f'TrueAPI вернул некорректный ответ: {response.text[:200]}',
        }

    # При unitedToken=True ЧЗ отдаёт uuidToken.
    token = result.get('uuidToken') or result.get('token')
    if not token:
        return {
            'uuid': row_uuid,
            'token': None,
            'message': (
                'TrueAPI не вернул токен (uuidToken/token). '
                f'Ответ: {str(result)[:200]}'
            ),
        }

    logger.info("Базовый токен сессии TrueAPI успешно получен")
    return {'uuid': row_uuid, 'token': token, 'message': 'Токен успешно получен.'}


def _extract_true_api_error(response) -> str:
    """Человекочитаемое сообщение об ошибке из ответа TrueAPI."""
    text = (response.text or '').strip()
    if text:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ('error_message', 'errorMessage', 'message', 'error', 'description'):
                value = data.get(key)
                if value:
                    text = str(value)
                    break
    if len(text) > 300:
        text = text[:300]
    return f'HTTP {response.status_code}: {text}' if text else f'HTTP {response.status_code}'


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
