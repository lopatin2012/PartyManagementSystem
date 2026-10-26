# app_cz/services/order_service.py

"""Просмотр и закрытие заказов кодов маркировки в СУЗ."""

import json
import logging

import requests

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_helper.sign_helper import unpinned_signed_data

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
MAX_BULK_CLOSE = 100


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

    account, error = _active_account()
    if error:
        return _error(error, page, limit)

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


def close_suz_order(order_id: str) -> dict:
    """
    Закрывает заказ (или все его подзаказы) в СУЗ.

    ``POST /api/v3/order/close?omsId=...``; тело ``{"orderId": ...}``
    подписывается откреплённой подписью в заголовке ``X-Signature``
    (см. API СУЗ 3.0, 4.4.8).

    :return: {'is_error': bool, 'message': str}.
    """
    order_id = (order_id or '').strip()
    if not order_id:
        return {'is_error': True, 'message': 'Не указан идентификатор заказа.'}

    account, error = _active_account()
    if error:
        return {'is_error': True, 'message': error}

    payload = {'orderId': order_id}
    json_data = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    try:
        _, signed_data = unpinned_signed_data(json_data)
    except Exception as e:
        logger.exception('СУЗ: не удалось подписать запрос закрытия заказа')
        return {'is_error': True, 'message': f'Ошибка подписи запроса: {e}'}

    headers = {
        'clientToken': account.dynamic_token,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-Signature': signed_data,
    }

    try:
        response = requests.post(
            SUZ.order_close,
            params={'omsId': account.oms_id},
            headers=headers,
            data=json_data,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f'СУЗ: ошибка закрытия заказа {order_id}: {e}')
        return {'is_error': True, 'message': f'Ошибка соединения с СУЗ: {e}'}

    if response.status_code != 200:
        message = _extract_error(response)
        logger.warning(
            f'СУЗ: закрытие заказа {order_id} вернуло '
            f'{response.status_code}: {message}'
        )
        return {
            'is_error': True,
            'message': f'СУЗ вернул ошибку {response.status_code}: {message}',
        }

    return {'is_error': False, 'message': f'Заказ {order_id} закрыт.'}


def close_suz_orders(order_ids) -> dict:
    """
    Закрывает несколько заказов кодов маркировки в СУЗ.

    Каждый заказ закрывается отдельным запросом ``/api/v3/order/close``
    (у метода нет пакетного режима). Ошибка одного заказа не прерывает
    остальные.

    :return: {'is_error', 'message', 'closed': [id], 'failed': [{'order_id', 'message'}]}.
    """
    ids = []
    seen = set()
    for raw in order_ids or []:
        order_id = str(raw or '').strip()
        if order_id and order_id not in seen:
            seen.add(order_id)
            ids.append(order_id)

    if not ids:
        return {
            'is_error': True,
            'message': 'Не выбрано ни одного заказа.',
            'closed': [],
            'failed': [],
        }
    if len(ids) > MAX_BULK_CLOSE:
        return {
            'is_error': True,
            'message': (
                f'За один раз можно закрыть не более {MAX_BULK_CLOSE} заказов.'
            ),
            'closed': [],
            'failed': [],
        }

    closed = []
    failed = []
    for order_id in ids:
        result = close_suz_order(order_id)
        if result.get('is_error'):
            failed.append({
                'order_id': order_id,
                'message': result.get('message', ''),
            })
        else:
            closed.append(order_id)

    return {
        'is_error': not closed,
        'message': (
            f'Закрыто заказов: {len(closed)}.'
            if closed else 'Ни один заказ не закрыт.'
        ),
        'closed': closed,
        'failed': failed,
    }


def _active_account():
    """Активная учётная запись СУЗ с omsId и динамическим токеном."""
    account = SUZAccount.objects.filter(is_active=True).first()
    if account is None:
        return None, 'Активная учётная запись СУЗ не найдена.'
    if not account.oms_id:
        return None, 'У активной учётной записи СУЗ не задан omsId.'
    if not account.dynamic_token:
        return None, (
            'Динамический токен СУЗ отсутствует — обновите его в панели статусов.'
        )
    return account, None


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
