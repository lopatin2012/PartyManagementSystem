# app_cz/services/true_api_client.py

"""
Клиент True API для получения токена аутентификации.

Токен получается по стандартному flow:
  1. GET /auth/key → {"uuid": "...", "data": "..."}
  2. Подпись data УКЭП (attached_signed_data)
  3. POST /auth/simpleSignIn → {"token": "..."} / {"uuidToken": "..."}

Используется для методов Национального каталога и других endpoints True API,
требующих Bearer-аутентификации.
"""

import logging

import requests

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_helper.sign_helper import attached_signed_data

logger = logging.getLogger(__name__)


class TrueAPIClient:
    """Клиент True API (методы Национального каталога и др.)."""

    def __init__(self, user=None):
        self.user = user
        self.base_url = SUZ.true_api
        self._token = None

    def get_token(self) -> str:
        """Получает токен аутентификации для методов True API.

        Использует стандартный flow: /auth/key → подпись → /auth/simpleSignIn.
        Токен кэшируется на уровне экземпляра (один запрос на сессию клиента).
        """
        if self._token:
            return self._token

        # 1. Получаем ключи для подписи.
        url_key = f'{self.base_url}/auth/key'
        response_key = requests.get(url_key, timeout=15)
        response_key.raise_for_status()
        auth_data = response_key.json()
        row_uuid = auth_data['uuid']
        row_data = auth_data['data']

        # 2. Подписываем данные прикреплённой подписью (ЭЦП).
        _, signed_data = attached_signed_data(row_data)

        # 3. Получаем токен через simpleSignIn.
        account = SUZAccount.objects.filter(is_active=True).first()
        if not account:
            raise ValueError('Активная учётная запись СУЗ не найдена')

        url_sign_in = f'{self.base_url}/auth/simpleSignIn'
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

        response = requests.post(url_sign_in, json=payload, headers=headers, timeout=15)
        response.raise_for_status()

        result = response.json()
        token = result.get('token') or result.get('uuidToken')
        if not token:
            raise ValueError('Токен отсутствует в ответе True API (simpleSignIn)')

        self._token = token
        logger.info('Токен True API успешно получен (simpleSignIn)')
        return token
