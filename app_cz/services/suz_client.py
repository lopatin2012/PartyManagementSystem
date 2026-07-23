# app_cz/services/suz_client.py

import requests
import logging

from app_cz.suz_config import SUZ

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