# app_cz/services/party_service.py
import json
import logging
import re
from datetime import datetime, date

import requests
from django.utils import timezone
from django.db.models import Q, ObjectDoesNotExist
from django.db import transaction

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_cz.enums import TypeProduct
from app_cz.services.suz_client import get_true_api_session_token, get_true_api_auth_key
from app_cz.services.code_client import send_application_report

from app_factory.models import ProductSKU

from app_helper.sign_helper import unpinned_signed_data

from app_uip.models import UIP, PartyStatusChoices

logger = logging.getLogger(__name__)


def validate_party_number(party_number: str) -> bool:
    """
    Валидация номера партии (УИП) согласно правилам Честного Знака.
    Формат: 14 цифр (GTIN) + 6 цифр (ГГММДД) + 1-12 символов (a-zA-Z0-9/.,-)
    """
    if not (21 <= len(party_number) <= 32):
        return False

    # Строгая проверка по регулярному выражению
    pattern = r'^\d{14}\d{6}[A-Za-z0-9/.,\-]{1,12}$'
    return bool(re.match(pattern, party_number))


def build_local_party_number(gtin: str, production_date: date, article: str) -> str:
    """
    Формирует локальный номер УИП:
    GTIN(14) + дата ГГММДД(6) + артикул(5) + добивка нулями до 32.
    Пример: 04601751029980260724143620000000
    """
    date_str = production_date.strftime('%y%m%d')
    article_part = (article or '')[:5].ljust(5, '0')
    base = f'{gtin}{date_str}{article_part}'  # 25 символов
    return base.ljust(32, '0')[:32]  # добивка до 32

def get_available_products() -> list[dict]:
    """Список продуктов, доступных для генерации УИП (с GTIN и артикулом)."""
    products = []
    skus = (
        ProductSKU.objects.filter(is_active=True)
        .select_related('product')
        .order_by('product__name', 'sku_code')
    )
    for sku in skus:
        gtin = sku.product.consumer_gtin
        if not gtin:
            # пропускаем продукты без GTIN потребительской упаковки.
            continue
        products.append({
            'sku_id': str(sku.id),
            'name': sku.product.name,
            'sku_code': sku.sku_code,
            'gtin': gtin,
            'group': sku.product.group,
        })
    return products





def generate_party_numbers(
        party_info_list: list[dict],
        product_group: str = TypeProduct.MILK.value,
) -> dict:
    """
    Генерируем номера партий со стороны Честного Знака.

    :param party_info_list: Список словарей [{'gtin': '...', 'productionDate': '...', 'count': 1}, ...]
    :param product_group: Товарная группа (например, 'milk', 'bio', 'null')
    :return: Словарь с результатом операции
    """
    if not party_info_list:
        logger.warning("Попытка генерации партий с пустым списком.")
        return {
            'is_error': True,
            'message_error': 'Список параметров для генерации пуст.'
        }

    if len(party_info_list) > 50:
        logger.warning("Попытка сгенерировать более 50 партий за один запрос.")
        return {
            'is_error': True,
            'message_error': 'Максимальное количество генераций за один запрос — 50.'
        }

    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    params = {}

    data = {
        'pg': product_group,
        'partyNumberInfo': party_info_list
    }
    json_dumps_data = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    try:
        logger.info(f"Запрос генерации партий в ЧЗ (группа: {product_group})")

        response = requests.post(
            SUZ.generation_party,
            headers=headers,
            params=params,
            data=json_dumps_data,
            timeout=15
        )

        # Проверка ответа.
        if response.status_code != 200:
            logger.error(f"Ошибка генерации партий {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        if not lst_party_info:
            logger.error(f"Отсутствуют данные о генерации в ответе: {response.text}")
            return {
                'is_error': True,
                'message_error': 'ЧЗ вернул пустой список партий.'
            }

        logger.info("Генерация партий успешно завершена.")
        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при генерации партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def reserve_parties_honest_sign(
        product_group: str,
        party_numbers: list[str]
) -> dict:
    """
    Резервируем свой список номеров партий в Честном Знаке.
    """
    if not party_numbers:
        return {
            'is_error': True,
            'message_error': 'Список партий для резервирования пуст.'
        }

    # 1. Валидация формата
    invalid_parties = [p for p in party_numbers if not validate_party_number(p)]
    if invalid_parties:
        return {
            'is_error': True,
            'message_error': f'Некорректный формат номеров партий: {", ".join(invalid_parties[:3])}...'
        }

    # 2. Проверка на дубликаты в нашей БД (используем новую модель UIP)
    existing_parties = set(
        UIP.objects.filter(
            number__in=party_numbers,
            status__in=UIP.ACTIVE_STATUSES
        ).values_list('number', flat=True)
    )

    available_to_reserve = [
        p
        for p in party_numbers
        if p not in existing_parties
    ]

    if not available_to_reserve:
        logger.warning("Все указанные УИП уже зарезервированы или находятся в работе.")
        return {
            'is_error': True,
            'message_error': 'Указанные УИП уже находятся в резерве или в работе.'
        }

    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    params = {}

    data = {
        'pg': product_group,
        'partyNumber': available_to_reserve
    }
    json_dumps_data = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    try:
        logger.info(f"Запрос резервирования {len(available_to_reserve)} партий в ЧЗ.")
        response = requests.post(
            SUZ.reservation_party,
            headers=headers,
            params=params,
            data=json_dumps_data,
            timeout=15
        )

        if response.status_code != 200:
            logger.error(f"Ошибка резервирования {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        logger.info("Резервирование партий успешно завершено.")
        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при резервировании партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def get_all_reserved_parties() -> dict:
    """
    Получает список всех зарезервированных партий со стороны ЧЗ.
    """
    # token для работы с TrueAPI.
    result_true_api_session_token = get_true_api_session_token()

    try:
        token = result_true_api_session_token.get('token')
        if not token:
            return {
                'is_error': True,
                'message_error': 'Не удалось получить токен сессии TrueAPI. Проверьте настройки СУЗ.'
            }
    except ValueError as e:
        return {
            'is_error': True,
            'message_error': str(e)
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        logger.info("Запрос списка зарезервированных партий из ЧЗ.")
        response = requests.get(
            SUZ.get_all_reserved_party,
            headers=headers,
            timeout=15
        )

        if response.status_code != 200:
            logger.error(f"Ошибка получения списка {response.status_code}: {response.text}")
            error_msg = (
                    response.json().get('errorMessage')
                    or response.json().get('error_message', 'Неизвестная ошибка ЧЗ')
            )
            return {
                'is_error': True,
                'message_error': error_msg
            }

        response_data = response.json()
        lst_party_info = response_data.get('partyNumberInfo', [])

        return {
            'is_error': False,
            'message_error': 'Ошибки отсутствуют',
            'lst_party_number_info': lst_party_info
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Сетевая ошибка при получении списка партий: {e}")
        return {
            'is_error': True,
            'message_error': f'Ошибка соединения с ЧЗ: {str(e)}'
        }


def close_party_reservation(
        cis: str,
        batch_number: str,
        is_marking_date: bool = False,
        marking_date: str = None,
        exp_date: str = None,
        exp_date_72: str = None,
) -> dict:
    """
    Снимает партию с резерва путем отправки отчета о нанесении.
    :param cis: Идентификатор продукта.
    :param batch_number: Номер закрываемой партии.
    :param is_marking_date: Флаг необходимости указания даты маркировки.
    :param marking_date: Дата маркировки.
    :param exp_date: Дата окончания срок годности.
    :param exp_date_72: Дата окончания срока годности (срок хранения продукта менее 72 часов).
    """
    # token для работы с TrueAPI.
    try:
        account = SUZAccount.objects.get(is_active=True)
        if not account.dynamic_token:
            raise ValueError("Динамический токен СУЗ отсутствует.")
    except ObjectDoesNotExist:
        return {
            'has_error': True,
            'status_close': False,
            'message': "Активная учётная запись СУЗ не найдена."
        }
    try:
        # 2. Отправляем отчёт о нанесении.
        report_result = send_application_report(
            sntins=[cis],
            batch_number=batch_number,
            is_marking_date=is_marking_date,
            marking_date=marking_date,
            exp_date=exp_date,
            exp_date_72=exp_date_72,
        )

        # 3. Обрабатываем результат.
        if report_result.get('has_error'):
            err_msg = report_result.get(
                'message',
                'Неизвестная ошибка отправки отчёта'
            )
            logger.error(f"Ошибка при закрытии партии {batch_number}: {err_msg}")
            return {
                'has_error': True,
                'status_close': False,
                'message': err_msg
            }

        # 4. Если отчёт отправлен успешно, обновляем статус УИП в нашей БД.
        uip = UIP.objects.filter(number=batch_number).first()
        if uip:
            uip.status = PartyStatusChoices.REGISTERED
            uip.save(update_fields=['status', 'updated_at'])
            logger.info(f"Партия {batch_number} успешно закрыта и переведена в статус REGISTERED.")
        else:
            logger.warning(f"УИП {batch_number} не найден в локальной БД, но отчёт в ЧЗ отправлен успешно.")

        return {
            'has_error': False,
            'status_close': True,
            'message': 'Зарезервированная партия успешно снята с резерва'
        }

    except ValueError as e:
        return {
            'has_error': True,
            'status_close': False,
            'message': str(e)
        }
    except Exception as e:
        logger.exception(f"Критическая ошибка при закрытии партии {batch_number}")
        return {
            'has_error': True,
            'status_close': False,
            'message': f'Внутренняя ошибка: {str(e)}'
        }


def sync_parties_from_cz() -> dict:
    """
    Синхронизирует список зарезервированных партий из Честного Знака с локальной базой.

    Returns:
        dict: {
            'is_error': bool,
            'message': str,
            'created': int,
            'updated': int,
            'total': int
        }
    """
    # Получаем список партий из ЧЗ
    result = get_all_reserved_parties()

    if result.get('is_error'):
        return {
            'is_error': True,
            'message': result.get(
                'message_error',
                'Ошибка получения данных из ЧЗ'
            ),
            'created': 0,
            'updated': 0,
            'total': 0
        }

    lst_party_info = result.get('lst_party_number_info', [])

    if not lst_party_info:
        return {
            'is_error': False,
            'message': 'В ЧЗ нет зарезервированных партий',
            'created': 0,
            'updated': 0,
            'total': 0
        }

    created_count = 0
    updated_count = 0

    try:
        with transaction.atomic():
            for party_info in lst_party_info:
                party_number = party_info.get('partyNumber', '')

                if not party_number:
                    logger.warning(
                        f'Пропущена запись без номера партии: {party_info}'
                    )
                    continue

                # Парсим дату производства (формат: YYMMDD из первых 6 цифр после GTIN).
                production_date = None
                if len(party_number) >= 20:
                    try:
                        date_str = party_number[14:20]  # 6 цифр после GTIN
                        production_date = datetime.strptime(date_str, '%y%m%d').date()
                    except ValueError:
                        pass

                # Определяем статус на основе данных из ЧЗ
                # По умолчанию считаем зарезервированным в ЧЗ.
                status = PartyStatusChoices.RESERVED_CZ

                # Пытаемся найти соответствующий SKU по GTIN.
                product_sku = None
                if len(party_number) >= 14:
                    gtin = party_number[:14]
                    product_sku = ProductSKU.objects.filter(
                        product__packagings__gtin=gtin,
                        is_active=True
                    ).first()

                # Создаём или обновляем УИП.
                uip, created = UIP.objects.update_or_create(
                    number=party_number,
                    defaults={
                        'product_sku': product_sku,
                        'status': status,
                        'production_date': production_date,
                        'reservation_date': timezone.now().date(),
                        'planned_quantity': party_info.get('expectedQuantity', 0),
                        'description': f'Синхронизировано из ЧЗ: '
                                       f'{timezone.now().strftime("%d.%m.%Y %H:%M")}'
                    }
                )

                if created:
                    created_count += 1
                    logger.info(f'Создан УИП: {party_number}')
                else:
                    updated_count += 1
                    logger.info(f'Обновлён УИП: {party_number}')

        message = (f'Синхронизация завершена. Создано: {created_count},'
                   f' обновлено: {updated_count}')
        logger.info(message)

        return {
            'is_error': False,
            'message': message,
            'created': created_count,
            'updated': updated_count,
            'total': len(lst_party_info)
        }

    except Exception as e:
        logger.error(
            f'Ошибка при синхронизации партий: {e}',
            exc_info=True
        )
        return {
            'is_error': True,
            'message': f'Ошибка при сохранении данных: {str(e)}',
            'created': created_count,
            'updated': updated_count,
            'total': len(lst_party_info)
        }

def _generate_local_uip(sku: ProductSKU, gtin: str, production_date: date) -> dict:
    """
    Создаёт УИП в локальном согласованном формате И резервирует его в ЧЗ.

    Собственный источник Номера партии: (GTIN + дата + артикул + нули),
    но резервирование всё равно проходит на стороне Честного Знака.
    """
    number = build_local_party_number(gtin, production_date, sku.sku_code)

    # 1. Проверка на дубликат в локальной БД.
    if UIP.objects.filter(number=number).exists():
        return {
            'is_error': True,
            'message': f'УИП с номером {number} уже существует.'
        }

    # 2. Резервируем сформированный номер в Честном Знаке.
    reserve_result = reserve_parties_honest_sign(
        product_group=sku.product.group,
        party_numbers=[number],
    )

    if reserve_result.get('is_error'):
        return {
            'is_error': True,
            'message': (
                f'ЧЗ отклонил резервирование номера: '
                f'{reserve_result.get("message_error", "неизвестная ошибка")}'
            )
        }

    # 3. ЧЗ подтвердил резервирование — сохраняем УИП локально.
    try:
        with transaction.atomic():
            UIP.objects.create(
                product_sku=sku,
                number=number,
                status=PartyStatusChoices.RESERVED_LOCAL,
                production_date=production_date,
                reservation_date=timezone.now().date(),
                description='Сгенерирован вручную (локальный формат), зарезервирован в ЧЗ',
            )
        logger.info(f'Создан и зарезервирован в ЧЗ локальный УИП: {number}')
        return {
            'is_error': False,
            'message': f'УИП сгенерирован и зарезервирован в ЧЗ: {number}',
            'number': number,
        }
    except Exception as e:
        logger.error(
            f'Ошибка сохранения локального УИП: {e}',
            exc_info=True
        )
        return {'is_error': True, 'message': f'Ошибка сохранения УИП: {str(e)}'}


def _generate_cz_uip(sku: ProductSKU, gtin: str, production_date: date) -> dict:
    """Генерирует УИП через Честный Знак (статус RESERVED_CZ)."""
    party_info = [{
        'gtin': gtin,
        'productionDate': production_date.strftime('%Y-%m-%d'),
        'count': 1,
    }]

    result = generate_party_numbers(
        party_info_list=party_info,
        product_group=sku.product.group,
    )

    if result.get('is_error'):
        return {
            'is_error': True,
            'message': result.get('message_error', 'Ошибка генерации в ЧЗ.')
        }

    lst = result.get('lst_party_number_info', [])
    if not lst:
        return {
            'is_error': True,
            'message': 'ЧЗ не вернул номера партий.'
        }

    created = []
    try:
        with transaction.atomic():
            for info in lst:
                number = info.get('partyNumber', '')
                if not number:
                    continue
                UIP.objects.update_or_create(
                    number=number,
                    defaults={
                        'product_sku': sku,
                        'status': PartyStatusChoices.RESERVED_CZ,
                        'production_date': production_date,
                        'reservation_date': timezone.now().date(),
                        'description': 'Сгенерирован вручную через Честный Знак',
                    }
                )
                created.append(number)

        if not created:
            return {
                'is_error': True,
                'message': 'Не удалось сохранить полученные номера.'
            }

        logger.info(f'Создано УИП через ЧЗ: {len(created)} шт.')
        return {
            'is_error': False,
            'message': f'Сгенерировано УИП через ЧЗ: {len(created)} шт.',
            'number': (
                created[0]
                if len(created) == 1
                else ', '.join(created),
            )
        }
    except Exception as e:
        logger.error(f'Ошибка сохранения УИП из ЧЗ: {e}', exc_info=True)
        return {
            'is_error': True,
            'message': f'Ошибка сохранения: {str(e)}'
        }


def generate_uip(product_sku_id: str, production_date: date, mode: str) -> dict:
    """
    Точка входа генерации УИП.
    mode: 'local' — локальный согласованный формат, 'cz' — через Честный Знак.
    """
    try:
        sku = ProductSKU.objects.select_related('product').get(
            id=product_sku_id, is_active=True
        )
    except ProductSKU.DoesNotExist:
        return {'is_error': True, 'message': 'Продукт не найден или неактивен.'}

    gtin = sku.product.consumer_gtin
    if not gtin:
        return {
            'is_error': True,
            'message': 'У продукта не заполнен GTIN потребительской упаковки.'
        }

    if mode == 'local':
        return _generate_local_uip(sku, gtin, production_date)
    if mode == 'cz':
        return _generate_cz_uip(sku, gtin, production_date)

    return {'is_error': True, 'message': 'Неизвестный режим генерации.'}