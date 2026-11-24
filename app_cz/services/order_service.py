# app_cz/services/order_service.py

"""Просмотр, скачивание кодов и закрытие заказов кодов маркировки в СУЗ."""

import json
import logging
from datetime import timedelta

import requests
from django.utils import timezone

from app_helper import external_http
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

# Размер порции при выгрузке кодов заказа. СУЗ отдаёт до 150 000 кодов
# за запрос; для одной выгрузки запрашиваем всё доступное количество, чтобы
# не терять уже выданные коды при сбое на середине.

# «Активные» заказы: ещё не закрыты и не отклонены. Используются как
# фильтр по умолчанию для страницы /suz/ (метод поиска принимает orderStatuses).
ACTIVE_ORDER_STATUSES = ('CREATED', 'PENDING', 'APPROVED', 'READY')


def order_status_label(status: str) -> str:
    """Код статуса заказа СУЗ → читаемая подпись."""
    return ORDER_STATUS_LABELS.get(status, status or '—')


def get_suz_providers() -> dict:
    """
    Список сервис-провайдеров СУЗ: ``GET /api/v3/providers`` (4.4.14).

    :return: {serviceProviderId: 'Наименование / ИНН'} (или {}) — чтобы
             показывать в таблице заказов имя вместо GUID.
    """
    from django.core.cache import cache

    cached = cache.get('suz_providers')
    if cached is not None:
        return cached

    account, error = _active_account()
    if error:
        logger.warning(f'СУЗ: сервис-провайдеры недоступны: {error}')
        return {}

    headers = {
        'clientToken': account.dynamic_token,
        'Accept': 'application/json',
    }
    try:
        response = external_http.get(
            'suz',
            SUZ.providers,
            params={'omsId': account.oms_id},
            headers=headers,
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f'СУЗ: ошибка получения сервис-провайдеров: {e}')
        return {}

    if response.status_code != 200:
        logger.warning(
            f'СУЗ: сервис-провайдеры вернули {response.status_code}: '
            f'{_extract_error(response)}'
        )
        return {}

    try:
        data = response.json()
    except ValueError:
        return {}

    providers = {}
    for item in data.get('providers') or []:
        provider_id = str(item.get('serviceProviderId') or '').strip()
        if not provider_id:
            continue
        name = str(item.get('providerName') or item.get('name') or '').strip()
        inn = str(item.get('taxIdentificationNumber') or '').strip()
        providers[provider_id] = (
            f'{name} / {inn}' if name and inn else (name or inn or provider_id)
        )

    cache.set('suz_providers', providers, 60 * 60)
    return providers


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
    if status == 'all':
        pass
    elif status in ORDER_STATUS_LABELS:
        order_filter['orderStatuses'] = [status]
    else:
        # По умолчанию — только активные заказы (без закрытых/отклонённых).
        order_filter['orderStatuses'] = list(ACTIVE_ORDER_STATUSES)
    if order_id:
        order_filter['orderIds'] = [order_id]

    payload = {'filter': order_filter, 'limit': limit, 'page': page}
    headers = {
        'clientToken': account.dynamic_token,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    try:
        response = external_http.post(
            'suz',
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


def get_suz_order_codes(order_id: str) -> dict:
    """
    Забирает доступные коды заказа из СУЗ и формирует TXT-файл.

    Внимание: ``GET /api/v3/codes`` выдаёт коды из заказа. После выгрузки
    они становятся недоступны для выдачи на линию, поэтому в интерфейсе
    действие подтверждается отдельно.

    :return: {'is_error', 'message', 'filename', 'content', 'count'}.
    """
    order_id = (order_id or '').strip()
    if not order_id:
        return {'is_error': True, 'message': 'Не указан идентификатор заказа.'}

    orders_result = get_suz_orders(
        page=1, limit=1, status='all', order_id=order_id,
    )
    if orders_result.get('is_error'):
        return {
            'is_error': True,
            'message': (
                orders_result.get('message')
                or 'Не удалось получить заказ из СУЗ.'
            ),
        }

    order = next(
        (
            item for item in (orders_result.get('orders') or [])
            if str(item.get('orderId') or '').lower() == order_id.lower()
        ),
        None,
    )
    if order is None:
        return {
            'is_error': True,
            'message': f'Заказ {order_id} не найден в СУЗ.',
        }

    # Скачивание — только для наших заказов, как и закрытие: заказы
    # сервис-провайдера нам не принадлежат.
    if str(order.get('serviceProviderId') or '').strip():
        return {
            'is_error': True,
            'message': (
                'Заказ создан через сервис-провайдера — скачивание кодов '
                'недоступно.'
            ),
        }
    if order.get('orderStatus') in ('CLOSED', 'DECLINED'):
        return {
            'is_error': True,
            'message': (
                'Заказ закрыт или отклонён — скачивание кодов недоступно.'
            ),
        }

    account, error = _active_account()
    if error:
        return {'is_error': True, 'message': error}

    # GTIN → сколько кодов ещё можно забрать. Буферы не в статусе ACTIVE
    # уже закрыты для выдачи (EXHAUSTED/CLOSED/REJECTED).
    targets = {}
    for buffer in order.get('buffers') or []:
        gtin = str(buffer.get('gtin') or '').strip()
        status = str(buffer.get('bufferStatus') or '').strip().upper()
        available = _buffer_available(buffer)
        if not gtin or available <= 0:
            continue
        if status and status != 'ACTIVE':
            continue
        targets[gtin] = targets.get(gtin, 0) + available

    if not targets:
        return {
            'is_error': True,
            'message': (
                'В заказе нет доступных для выдачи кодов. '
                'Возможно, коды уже выданы.'
            ),
        }

    codes = []
    for gtin, available in targets.items():
        data, error = _fetch_order_codes(
            account=account,
            order_id=order_id,
            gtin=gtin,
            quantity=available,
        )
        if error:
            return {'is_error': True, 'message': error}

        batch = data.get('codes') if isinstance(data, dict) else None
        if isinstance(batch, dict):
            batch = [batch.get('code')] if batch.get('code') else []
        if not isinstance(batch, list):
            batch = []
        batch = [str(code).strip() for code in batch]
        batch = [code for code in batch if code]
        codes.extend(batch)

    if not codes:
        return {
            'is_error': True,
            'message': 'СУЗ не вернул коды заказа.',
        }

    from django.utils import timezone

    if len(targets) == 1:
        file_prefix = next(iter(targets))
    else:
        file_prefix = order_id
    filename = (
        f'{file_prefix}_{len(codes)}_'
        f'{timezone.now().strftime("%d%m%y")}.txt'
    )
    # Коды пишем как пришли из СУЗ: управляющий разделитель GS (\x1d)
    # между КИ и крипто-хвостом обязателен для печати/сканирования.
    content = ''.join(f'{str(code)}\r\n' for code in codes)
    return {
        'is_error': False,
        'message': f'Скачано кодов: {len(codes)}.',
        'filename': filename,
        'content': content,
        'count': len(codes),
        'gtins': list(targets),
    }


def _buffer_available(buffer: dict) -> int:
    """Сколько кодов ещё доступно в буфере заказа."""
    return max(_as_int(buffer.get('availableCodes'), 0), 0)


def _fetch_order_codes(
        account: SUZAccount,
        order_id: str,
        gtin: str,
        quantity: int,
) -> tuple[dict | None, str | None]:
    """Получить доступные коды заказа: ``GET /api/v3/codes``."""
    headers = {
        'clientToken': account.dynamic_token,
        'Accept': 'application/json',
    }
    params = {
        'omsId': account.oms_id,
        'orderId': order_id,
        'quantity': quantity,
        'gtin': gtin,
    }
    try:
        response = external_http.get(
            'suz',
            SUZ.download_codes_from_order,
            params=params,
            headers=headers,
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f'СУЗ: ошибка получения кодов заказа {order_id}: {e}')
        return None, f'Ошибка соединения с СУЗ: {e}'

    if response.status_code != 200:
        message = _extract_error(response)
        logger.warning(
            f'СУЗ: получение кодов заказа {order_id} (GTIN {gtin}) вернуло '
            f'{response.status_code}: {message}'
        )
        return None, f'СУЗ вернул ошибку {response.status_code}: {message}'

    try:
        data = response.json()
    except ValueError:
        return None, 'СУЗ вернул некорректный ответ (не JSON).'

    return data, None


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
        response = external_http.post(
            'suz',
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


def check_suz_active_orders_and_notify() -> dict:
    """
    Проверяет число активных заказов кодов в СУЗ и уведомляет по почте
    при превышении порога ``SUZ_ACTIVE_ORDERS_LIMIT`` (по умолчанию 90).

    По аналогии с письмом о сгорании УИП: текст + HTML с таблицей,
    получатели — ``SUZ_ORDERS_NOTIFICATION_EMAILS`` (fallback на
    ``UIP_RESERVE_NOTIFICATION_EMAILS``), отправка через ``_notify``.
    """
    from django.conf import settings

    limit = _as_int(getattr(settings, 'SUZ_ACTIVE_ORDERS_LIMIT', 90), 90)

    result = get_suz_orders(page=1, limit=1)
    if result.get('is_error'):
        message = (
            'СУЗ: не удалось проверить количество активных заказов: '
            f'{result.get("message")}'
        )
        logger.warning(message)
        return {
            'is_error': True,
            'count': 0,
            'limit': limit,
            'notified': False,
            'message': message,
        }

    count = _as_int(result.get('total_count'), 0)
    summary = {
        'is_error': False,
        'count': count,
        'limit': limit,
        'notified': False,
        'message': '',
    }

    if count <= limit:
        summary['message'] = (
            f'Активных заказов СУЗ: {count} (порог {limit}) — норма.'
        )
        logger.info(summary['message'])
        return summary

    orders_result = get_suz_orders(page=1, limit=min(count, MAX_PAGE_SIZE))
    orders = []
    if not orders_result.get('is_error'):
        orders = orders_result.get('orders') or []

    recipients = getattr(settings, 'SUZ_ORDERS_NOTIFICATION_EMAILS', []) or []
    subject = (
        f'ВНИМАНИЕ: активных заказов кодов в СУЗ {count} '
        f'(порог {limit})'
    )
    body, html_message = _build_orders_email(orders, count, limit)

    from app_cz.services.reserve_monitor import _notify

    sent = _notify(
        subject, body, recipients, html_message=html_message,
    )
    summary['notified'] = bool(sent.get('sent'))
    summary['is_error'] = not summary['notified']
    summary['message'] = (
        f'Активных заказов СУЗ: {count} > {limit}. '
        f'Письмо отправлено: {summary["notified"]}.'
    )
    logger.info(summary['message'])
    return summary


def _build_orders_email(orders: list, count: int, limit: int) -> tuple[str, str]:
    """Собирает (text, html) письма о превышении числа активных заказов."""
    from html import escape

    title = f'Активных заказов кодов в СУЗ: {count} (порог {limit}).'
    note = (
        'У СУЗ действует лимит 100 активных заказов. Закройте неиспользуемые '
        'заказы в разделе «СУЗ», чтобы не упереться в лимит.'
    )

    # Текстовая версия — fallback для клиентов без HTML.
    text_lines = [title, '', note, '']
    for order in orders:
        buffers = order.get('buffers') or []
        total_codes = sum(_as_int(b.get('totalCodes'), 0) for b in buffers)
        text_lines.append(
            f'Заказ {order.get("orderId", "—")}'
        )
        text_lines.append(
            f'  Создан: {_format_created(order.get("createdTimestamp"))} · '
            f'Статус: {order_status_label(order.get("orderStatus"))} · '
            f'КМ: {total_codes}'
        )
        text_lines.append('')
    text_lines.append('Система управления партиями PartyManagementSystem')
    text = '\n'.join(text_lines)

    # HTML-версия.
    text_color = '#202124'
    muted = '#5f6368'
    border = '#e0e0e0'
    danger = '#d93025'

    head_cells = ''.join(
        f'<th style="padding:8px 12px;text-align:left;font-size:12px;'
        f'text-transform:uppercase;letter-spacing:0.03em;color:{muted};'
        f'border-bottom:2px solid {border}">{label}</th>'
        for label in ('Заказ', 'Создан', 'Статус', 'КМ')
    )
    rows_html = ''.join(
        '<tr>'
        f'<td style="padding:8px 12px;border-bottom:1px solid {border};'
        f'font-family:Consolas,Menlo,monospace;font-size:12px;'
        f'color:{text_color};word-break:break-all">'
        f'{escape(str(order.get("orderId", "—")))}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid {border};'
        f'font-size:13px;color:{muted};white-space:nowrap">'
        f'{_format_created(order.get("createdTimestamp"))}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid {border};'
        f'font-size:13px;color:{text_color};white-space:nowrap">'
        f'{escape(order_status_label(order.get("orderStatus")))}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid {border};'
        f'font-size:13px;color:{text_color};text-align:center">'
        f'{sum(_as_int(b.get("totalCodes"), 0) for b in (order.get("buffers") or []))}'
        f'</td>'
        '</tr>'
        for order in orders
    )

    html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;'
        f'color:{text_color};max-width:900px">'
        f'<div style="background:{danger};color:#ffffff;padding:16px 20px;'
        f'border-radius:8px 8px 0 0;font-size:18px;font-weight:700">'
        f'&#9888; {escape(title)}</div>'
        f'<div style="border:1px solid {border};border-top:none;'
        f'border-radius:0 0 8px 8px;padding:16px 18px">'
        f'<p style="margin:0 0 14px;font-size:14px;color:{muted}">'
        f'{escape(note)}</p>'
        f'<table style="border-collapse:collapse;width:100%">'
        f'<thead><tr>{head_cells}</tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        f'<p style="color:{muted};font-size:12px;margin:18px 0 0">'
        f'Система управления партиями PartyManagementSystem</p>'
        f'</div></div>'
    )
    return text, html


def _format_created(timestamp_ms) -> str:
    """Unix-время в миллисекундах → «дд.мм.гггг чч:мм» (или «—»)."""
    from datetime import datetime

    from django.utils import timezone

    ms = _as_int(timestamp_ms, 0)
    if not ms:
        return '—'
    moment = datetime.fromtimestamp(
        ms / 1000, tz=timezone.get_current_timezone(),
    )
    return moment.strftime('%d.%m.%Y %H:%M')


def _active_account():
    """Активная учётная запись СУЗ с omsId и действующим динамическим токеном."""
    account = SUZAccount.objects.filter(is_active=True).first()
    if account is None:
        return None, 'Активная учётная запись СУЗ не найдена.'
    if not account.oms_id:
        return None, 'У активной учётной записи СУЗ не задан omsId.'

    # Обновляем токен заранее, если он на исходе: плановая задача может не
    # успеть (воркер/health недоступны).
    if (
        account.dynamic_token
        and account.token_expires_at
        and account.token_expires_at <= timezone.now() + timedelta(hours=1)
    ):
        from app_cz.services.suz_client import ensure_suz_token_valid

        ensure_suz_token_valid()
        account.refresh_from_db()

    if not account.dynamic_token:
        return None, (
            'Динамический токен СУЗ отсутствует — обновите его в панели статусов.'
        )
    if account.token_expires_at and account.token_expires_at <= timezone.now():
        return None, (
            'Динамический токен СУЗ истёк — обновите его в панели статусов.'
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
        for key in ('globalErrors', 'fieldErrors'):
            errors = data.get(key)
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    return str(
                        first.get('error')
                        or first.get('fieldError')
                        or first
                    )[:300]
                return str(first)[:300]
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
