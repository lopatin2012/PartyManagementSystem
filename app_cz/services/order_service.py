# app_cz/services/order_service.py

"""Просмотр заказов кодов маркировки в СУЗ."""

import logging

import requests

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ

logger = logging.getLogger(__name__)

# Статусы заказа СУЗ (справочник orderStatus) → подписи для интерфейса.
ORDER_STATUS_LABELS = {
    'CREATED': 'Создан',
    'PENDING': 'Ожидает подтверждения',
    'DECLINED': 'Отклонён',
    'APPROVED': 'Подтверждён',
    'READY': 'Доступен',
    'CLOSED': 'Закрыт',
}

MAX_PAGE_SIZE = 100


def order_status_label(status: str) -> str:
    """Код статуса заказа СУЗ → читаемая подпись."""
    return ORDER_STATUS_LABELS.get(status, status or '—')


def get_suz_orders(
        page: int = 1,
        limit: int = 50,
        status: str = None,
        order_id: str = None,
) -> dict:
    """
    Возвращает текущие заказы кодов маркировки из СУЗ.

    Использует ``POST /api/v3/orders/search`` с ``clientToken`` активной
    учётной записи СУЗ и ``omsId`` в query string (см. API СУЗ 3.0, 4.4.29).

    :return: {'is_error', 'message', 'total_count', 'orders', 'page', 'limit'}.
    """
    page = max(1, _as_int(page, 1))
    limit = max(1, min(_as_int(limit, 50), MAX_PAGE_SIZE))

    account = SUZAccount.objects.filter(is_active=True).first()
    if account is None:
        return _error('Активная учётная запись СУЗ не найдена.', page, limit)
    if not account.oms_id:
        return _error('У активной учётной записи СУЗ не задан omsId.', page, limit)
    if not account.dynamic_token:
        return _error(
            'Динамический токен СУЗ отсутствует — обновите его в панели статусов.',
            page, limit,
        )

    order_filter = {}
    if status in ORDER_STATUS_LABELS:
        order_filter['orderStatuses'] = [status]
    if order_id:
        order_filter['orderIds'] = [order_id]

    payload = {'filter': order_filter, 'limit': limit, 'page': page}
    headers = {
        'clientToken': account.dynamic_token,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(
            SUZ.order_search,
            params={'omsId': account.oms_id},
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f'СУЗ: ошибка получения заказов: {e}')
        return _error(f'Ошибка соединения с СУЗ: {e}', page, limit)

    if response.status_code != 200:
        message = _extract_error(response)
        logger.warning(
            f'СУЗ: получение заказов вернуло {response.status_code}: {message}'
        )
        return _error(
            f'СУЗ вернул ошибку {response.status_code}: {message}', page, limit,
        )

    try:
        data = response.json()
    except ValueError:
        return _error('СУЗ вернул некорректный ответ (не JSON).', page, limit)

    orders = data.get('results') or []
    return {
        'is_error': False,
        'message': '',
        'total_count': _as_int(data.get('totalCount'), len(orders)),
        'orders': orders,
        'page': page,
        'limit': limit,
    }


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_error(response) -> str:
    """Достаёт текст ошибки из ответа СУЗ (JSON или plain text)."""
    try:
        data = response.json()
    except ValueError:
        return (response.text or '')[:300]
    if isinstance(data, dict):
        return str(
            data.get('message')
            or data.get('errorMessage')
            or data.get('error_message')
            or data
        )[:300]
    return str(data)[:300]


def _error(message: str, page: int, limit: int) -> dict:
    return {
        'is_error': True,
        'message': message,
        'total_count': 0,
        'orders': [],
        'page': page,
        'limit': limit,
    }
