# app_cz/services/code_sync.py

import logging

import requests
from django.db import transaction

from app_cz.models import (
    CISCode,
    CISCodesStatusChoices,
    ProductionCodeStatusChoices
)
from app_uip.models import ProductionParty
from app_factory.models import ProductPackaging, PackagingLevelChoices

logger = logging.getLogger(__name__)


def _map_production_status(working_code: dict) -> int:
    """
    Преобразует статусы в production_status нового проекта.

    Логика:
    - Если status_application = True → APPLIED (нанесён)
    - Иначе → FREE (свободный)
    """
    if working_code.get('status_application'):
        return ProductionCodeStatusChoices.APPLIED
    else:
        return ProductionCodeStatusChoices.FREE


def _map_cz_status(code: dict) -> int:
    """
    Преобразует булевы флаги статуса из рабочего проекта в IntegerChoices нового.

    Логика приоритета (от высшего к низшему):
    1. status_introduction_into_circulation → INTRODUCED_INTO_CIRCULATION
    2. status_application → APPLIED
    3. status_emission_lived → EMITTED
    4. status_delete → WITHDRAWN_FROM_CIRCULATION (или пропускаем)
    5. По умолчанию → EMITTED
    """
    if code.get('status_introduction_into_circulation'):
        return CISCodesStatusChoices.INTRODUCED_INTO_CIRCULATION
    elif code.get('status_application'):
        return CISCodesStatusChoices.APPLIED
    elif code.get('status_delete') or code.get('laboratory'):
        return CISCodesStatusChoices.WITHDRAWN_FROM_CIRCULATION
    elif code.get('status_emission_lived'):
        return CISCodesStatusChoices.EMITTED
    else:
        return CISCodesStatusChoices.EMITTED


def sync_codes_task(
        url: str,
        task_id: str,
        production_party_id: str,
        packaging_id: str,
        token: str = None,
        level: int = PackagingLevelChoices.UNIT,
) -> dict:
    """
    Получает коды и создает локально.

    :param url: URL API endpoint (например, http://192.168.1.100:8000)
    :param task_id: ID задания (Task) в рабочем проекте
    :param production_party_id: ID ProductionParty в новом проекте
    :param packaging_id: ID ProductPackaging в новом проекте
    :param token: Токен авторизации для API рабочего проекта (если требуется)
    :param level: Уровень упаковки для всех кодов
    :return: Словарь с результатом синхронизации
    """

    # 1. Проверка существования записей в новом проекте.
    try:
        party = ProductionParty.objects.get(id=production_party_id)
        packaging = ProductPackaging.objects.get(id=packaging_id)
    except (ProductionParty.DoesNotExist, ProductPackaging.DoesNotExist) as e:
        return {
            'has_error': True,
            'message': f'Запись не найдена в новом проекте: {str(e)}'
        }

    # 2. Формируем URL для запроса кодов из рабочего проекта
    # В проекте должен быть API endpoint: /codes/api/get_codes_by_task/?task_id={task_id}
    api_url = f"{url.rstrip('/')}/codes/api/get_codes_by_task/"
    params = {'task_id': str(task_id)}

    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Token {token}'

    # 3. Запрашиваем коды.
    try:
        logger.info(f"Запрос кодов из рабочего проекта: task_id={task_id}")
        response = requests.get(api_url, params=params, headers=headers, timeout=30)
        response.raise_for_status()

        codes = response.json()

        if not codes:
            return {
                'has_error': False,
                'message': 'Коды не найдены в рабочем проекте',
                'synced_count': 0
            }

        logger.info(f"Получено {len(codes)} кодов из рабочего проекта")

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к рабочему проекту: {e}")
        return {
            'has_error': True,
            'message': f'Не удалось получить коды из рабочего проекта: {str(e)}'
        }

    # 4. Преобразуем и создаем коды.
    new_codes_to_create = []
    skipped_count = 0

    incoming_codes = [c.get('code') for c in codes if c.get('code')]
    existing_codes_set = set(
        CISCode.objects.filter(code__in=incoming_codes).values_list('code', flat=True)
    )

    for code in codes:
        code_str = code.get('code')

        if not code_str:
            skipped_count += 1
            continue

        # Проверяем, существует ли уже такой код.
        if code_str in existing_codes_set:
            skipped_count += 1
            continue

        # Маппинг статусов.
        cz_status = _map_cz_status(code)
        production_status = _map_production_status(code)

        # Создаем экземпляр новой модели.
        new_code = CISCode(
            production_party=party,
            product_packaging=packaging,
            code=code_str,
            level=level,
            cz_status=cz_status,
            production_status=production_status,
            parent=None,  # Агрегация пока не поддерживается.
        )

        new_codes_to_create.append(new_code)

    # 5. Пакетная вставка в БД.
    if new_codes_to_create:
        try:
            with transaction.atomic():
                created = CISCode.objects.bulk_create(
                    new_codes_to_create,
                    batch_size=5000,
                    ignore_conflicts=True
                )

                logger.info(f"Успешно создано {len(created)} кодов в новом проекте")

                return {
                    'has_error': False,
                    'message': f'Успешно синхронизировано {len(created)} кодов',
                    'synced_count': len(created),
                    'skipped_count': skipped_count
                }
        except Exception as e:
            logger.exception(f"Ошибка при создании кодов: {e}")
            return {'has_error': True, 'message': f'Ошибка сохранения в БД: {str(e)}'}
    else:
        return {
            'has_error': False,
            'message': 'Все коды уже существуют или были пропущены',
            'synced_count': 0,
            'skipped_count': skipped_count
        }
