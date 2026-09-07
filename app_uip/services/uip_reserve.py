# app_uip/services/uip_reserve.py

import logging
from datetime import date as date_type
from datetime import datetime

from app_cz.services.party_service import (
    generate_uip,
    reserve_parties_honest_sign,
    find_sku_by_gtin,
)
from app_factory.models import ProductSKU

logger = logging.getLogger(__name__)


def _parse_date(value):
    """Приводит дату к date; принимает date, datetime или строку ГГГГ-ММ-ДД."""
    if isinstance(value, date_type):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    return None


def _resolve_product_sku(item: dict):
    """
    Находит SKU по артикулу или GTIN (потребительская упаковка).
    Возвращает (product_sku, error_message).
    """
    article = item.get('article')
    gtin = item.get('gtin')

    if article:
        sku = ProductSKU.objects.filter(
            article=article, is_active=True
        ).select_related('product').first()
        if sku:
            return sku, None

    if gtin:
        sku = find_sku_by_gtin(gtin)
        if sku:
            return sku, None

    return None, 'Продукт не найден или неактивен (укажите article или gtin).'


def _reserve_generate(item: dict, is_external_service: bool) -> dict:
    """
    Генерирует (и резервирует) УИП через единый внутренний генератор generate_uip.
    Поддерживает count — количество УИП для генерации.
    """
    product_sku, error = _resolve_product_sku(item)
    if not product_sku:
        return {'is_error': True, 'message': error}

    production_date = _parse_date(item.get('production_date'))
    if not production_date:
        return {'is_error': True, 'message': 'Не указана или некорректна дата производства (production_date).'}

    mode = item.get('mode', 'local')
    party = item.get('party')
    target_status = item.get('target_status')
    skip_cz = item.get('skip_cz', False)
    count = int(item.get('count') or 1)
    if count < 1:
        count = 1
    if count > 50:
        return {'is_error': True, 'message': 'Максимальное количество УИП за один запрос — 50.'}

    results = []
    for _ in range(count):
        kwargs = dict(
            product_sku=product_sku,
            production_date=production_date,
            mode=mode,
            is_external_service=is_external_service,
            target_status=target_status,
            skip_cz=skip_cz,
        )
        if party:
            kwargs['party'] = party
        result = generate_uip(**kwargs)
        results.append(result)
        if result.get('is_error'):
            break

    if not results:
        return {'is_error': True, 'message': 'Не удалось сгенерировать УИП.'}

    # Если все успешны — возвращаем обобщённый результат.
    errors = [r for r in results if r.get('is_error')]
    if errors:
        return {
            'is_error': True,
            'message': errors[0].get('message', 'Ошибка генерации УИП'),
            'results': results,
        }

    numbers = [r.get('number') for r in results if r.get('number')]
    return {
        'is_error': False,
        'number': numbers[0] if len(numbers) == 1 else ', '.join(numbers),
        'numbers': numbers,
        'uuid_uip': results[0].get('uuid_uip'),
        'count': len(results),
        'message': f'Сгенерировано УИП: {len(results)} шт.',
        'results': results,
    }


def _reserve_own(item: dict) -> dict:
    """
    Резервирует уже сформированные номера УИП через reserve_parties_honest_sign.
    """
    product_group = item.get('product_group')
    party_numbers = item.get('party_numbers') or []

    if not product_group:
        return {'is_error': True, 'message': 'Не указана товарная группа (product_group).'}
    if not party_numbers:
        return {'is_error': True, 'message': 'Не указаны номера партий (party_numbers).'}

    result = reserve_parties_honest_sign(
        product_group=product_group,
        party_numbers=party_numbers,
    )

    if result.get('is_error'):
        return {
            'is_error': True,
            'message': result.get('message_error', 'Ошибка резервирования в ЧЗ'),
        }

    lst = result.get('lst_party_number_info', [])
    numbers = [
        info.get('partyNumber')
        for info in lst
        if info.get('partyNumber')
    ]
    return {
        'is_error': False,
        'number': ', '.join(numbers),
        'numbers': numbers,
        'count': len(numbers),
        'message': f'Зарезервировано номеров: {len(numbers)} шт.',
    }


def reserve_uips(items, is_external_service: bool = False) -> dict:
    """
    Общий внешний метод резервирования партий в Честном Знаке.

    Принимает как один словарь, так и список словарей. Каждый элемент — запрос
    одного из двух типов:

    - Генерация нового УИП (через внутренний generate_uip):
        {
            "article": "...",          # или "gtin"
            "production_date": "ГГГГ-ММ-ДД",
            "mode": "local" | "cz",
            "count": 1,                # опционально, сколько УИП сгенерировать
            "party": "000",            # опционально (только для local)
            "target_status": "...",    # опционально
            "skip_cz": false,          # опционально (черновик, только local)
        }

    - Резервирование своих номеров:
        {
            "product_group": "milk",
            "party_numbers": ["...", "..."]
        }

    Все резервирования при генерации проходят через generate_uip.
    """
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return {
            'is_error': True,
            'message': 'Запрос должен быть объектом или списком объектов.'
        }
    if not items:
        return {
            'is_error': True,
            'message': 'Пустой список запросов на резервирование.'
        }

    results = []
    for item in items:
        if not isinstance(item, dict):
            results.append({
                'is_error': True,
                'message': 'Элемент запроса должен быть объектом.'
            })
            continue

        # Резервирование своих номеров.
        if 'party_numbers' in item:
            results.append(_reserve_own(item))
        else:
            results.append(_reserve_generate(item, is_external_service))

    errors = [r for r in results if r.get('is_error')]
    total_count = sum(r.get('count', 0) or 0 for r in results)

    return {
        'is_error': bool(errors),
        'message': (
            f'Обработано запросов: {len(results)}, успешно зарезервировано: {total_count} шт.'
            if not errors
            else f'Часть запросов завершилась с ошибкой ({len(errors)} из {len(results)}).'
        ),
        'count': total_count,
        'results': results,
    }
