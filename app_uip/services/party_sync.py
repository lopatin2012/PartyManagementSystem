# app_uip/services/party_sync.py

import logging
import requests

from datetime import datetime, time

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

import requests

from app_factory.models import Line

from app_uip.models import UIP, ProductionParty, ProductionPartyStatusChoices

logger = logging.getLogger(__name__)

# ==========================================
# Маппинг и парсеры.
# ==========================================
PRODUCTION_STATUS_MAP = {
    'Проверка': ProductionPartyStatusChoices.CHECK,
    'Создан': ProductionPartyStatusChoices.CREATED,
    'В работе': ProductionPartyStatusChoices.WORK,
    'Закрыто': ProductionPartyStatusChoices.CLOSED,
    'Завершено': ProductionPartyStatusChoices.COMPLETED,
    'Удалено': ProductionPartyStatusChoices.DELETED,
    'Архив': ProductionPartyStatusChoices.ARCHIVED,
    'Ошибка': ProductionPartyStatusChoices.ERROR,
}

def map_production_status(raw: str):
    """Маппинг статуса из внешнего сервиса на внутренний код."""
    if not raw:
        return ProductionPartyStatusChoices.CREATED
    # Пробуем как код, потом как русское название.
    return (
        ProductionPartyStatusChoices(raw).value
        if raw in ProductionPartyStatusChoices.values
        else PRODUCTION_STATUS_MAP.get(
            raw.strip(), ProductionPartyStatusChoices.ERROR
        )
    )

def parse_int(value) -> int:
    """Безопасный парсинг целого (значения приходят строками)."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return 0

def parse_dt(value: str):
    """ISO datetime → timezone-aware datetime (или None)."""
    dt = parse_datetime(value) if value else None
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt

def parse_date_as_dt(value: str):
    """ISO date → datetime (начало дня, timezone-aware) или None."""
    d = parse_date(value) if value else None
    if not d:
        return None
    dt = datetime.combine(d, time.min)
    return timezone.make_aware(dt)

def find_uip_for_party(item: dict):
    """
    Определение УИП для производственной партии.
    :param item:
    :return:
    """

    product_uuid = item.get('product_uuid')
    marking = parse_date(item.get('date_marking') or '')
    if product_uuid and marking:
        return UIP.objects.filter(
            product_sku_id=product_uuid,
            production_date=marking,
        ).first()

    return None

# ==========================================
# Клиент внешнего сервиса.
# ==========================================

def fetch_production_parties() -> dict:
    """Запрашивает список производственных партий у внешнего сервиса."""
    url = getattr(settings, 'EXTERNAL_PARTIES_URL', None)
    if not url:
        return {
            'is_error': True,
            'message': 'Не настроен EXTERNAL_PARTIES_URL',
            'items': []
        }

    try:
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            return {
                'is_error': True,
                'message': f'Внешний сервис вернул {response.status_code}',
                'items': [],
            }
        data = response.json()

        items = data if isinstance(data, list) else data.get('results', data.get('data', []))
        return {'is_error': False, 'message': 'OK', 'items': items}
    except requests.exceptions.RequestException as e:
        logger.error(f'Ошибка запроса к внешнему сервису: {e}')
        return {'is_error': True, 'message': f'Ошибка соединения: {str(e)}', 'items': []}
