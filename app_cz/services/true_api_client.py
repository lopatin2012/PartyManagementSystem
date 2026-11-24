# app_cz/services/true_api_client.py

"""
Клиент True API (методы Национального каталога и др.).

Единый UUID-токен получает и кэширует `suz_client.get_true_api_session_token`:
он хранится в `SUZAccount`, обновляется под `SELECT FOR UPDATE` и
переиспользуется до истечения. Клиент не дублирует auth-flow и не держит
собственную копию токена — иначе длинная синхронизация (больше 10 часов)
использовала бы просроченный токен.
"""

import logging

from app_cz.suz_config import SUZ
from app_cz.services.suz_client import get_true_api_session_token

logger = logging.getLogger(__name__)


class TrueAPIClient:
    """Клиент True API (методы Национального каталога и др.)."""

    def __init__(self, user=None):
        self.user = user
        self.base_url = SUZ.true_api

    def get_token(self) -> str:
        """Возвращает действующий единый UUID-токен True API (из общего кэша).

        Проверка срока выполняется при каждом вызове, поэтому токен не
        «застревает» в клиенте на время долгого прогона.
        """
        result = get_true_api_session_token() or {}
        token = result.get('token')
        if not token:
            raise ValueError(
                result.get('message') or 'Токен отсутствует в ответе True API'
            )
        return token
