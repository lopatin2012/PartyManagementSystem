# app_cz/services/party_service.py
import json
import logging
import re
from datetime import datetime, date

import requests
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import ObjectDoesNotExist
from django.db import transaction
from django.utils.dateparse import parse_datetime, parse_date

from app_cz.models import SUZAccount
from app_cz.suz_config import SUZ
from app_cz.enums import TypeProduct
from app_cz.services.suz_client import get_true_api_session_token, get_true_api_auth_key
from app_cz.services.code_client import send_application_report

from app_factory.models import ProductSKU, Product, PackagingLevelChoices

from app_helper.sign_helper import unpinned_signed_data

from app_uip.models import UIP, PartyStatusChoices, UIPStatusLog

logger = logging.getLogger(__name__)

# Максимальное количество записей в одном запросе.
MAX_BATCH_SIZE = 50


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


def parse_cz_datetime(value: str):
    """
    Парсит дату-время из ЧЗ (ISO 8601).
    Пример: '2026-07-30T22:00:00.000Z' -> datetime.
    """
    if not value:
        return None
    # Заменяем Z на +00:00 для гарантированной совместимости.
    return parse_datetime(value.replace('Z', '+00:00'))


def parse_cz_date(value: str):
    """
    Парсит дату из ЧЗ (YYYY-MM-DD).
    Пример: '2026-07-30' -> date.
    """
    if not value:
        return None
    return parse_date(value)


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


def find_sku_by_gtin(gtin: str):
    """Ищет активный SKU по GTIN потребительской упаковки."""
    product = Product.objects.filter(
        packagings__gtin=gtin,
        packagings__level=PackagingLevelChoices.UNIT,
        is_active=True,
    ).first()

    if not product:
        return None

    return product.skus.filter(is_active=True).first()


def get_available_products() -> list[dict]:
    """Список продуктов, доступных для генерации УИП (с GTIN и артикулом)."""
    products = []
    skus = (
        ProductSKU.objects.filter(is_active=True)
        .select_related('product')
        .order_by('product__name', 'article')
    )
    for sku in skus:
        gtin = sku.product.consumer_gtin
        if not gtin:
            # пропускаем продукты без GTIN потребительской упаковки.
            continue
        products.append({
            'sku_id': str(sku.id),
            'name': sku.product.name,
            'article': sku.article,
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

    # 1. Валидация формата.
    invalid_parties = [p for p in party_numbers if not validate_party_number(p)]
    if invalid_parties:
        return {
            'is_error': True,
            'message_error': f'Некорректный формат номеров партий: {", ".join(invalid_parties[:3])}...'
        }

    # 2. Проверка на дубликаты.
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
            changed = uip.change_status(
                PartyStatusChoices.REGISTERED,
                source='service',
                note='Отчёт о нанесении отправлен, партия снята с резерва',
            )
            if changed:
                logger.info(f"Партия {batch_number} переведена в статус REGISTERED.")
            else:
                logger.info(f"Партия {batch_number} уже находилась в статусе REGISTERED.")
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
    Синхронизирует список зарезервированных партий из ЧЗ с локальной базой.

    - Новый УИП → создаём RESERVED_CZ, ставим reservation_date (первое резервирование).
    - RESERVED_CZ / RESERVED_LOCAL → норма: актуализируем данные, статус не меняем.
    - Любой другой статус при наличии в резерве ЧЗ → РАССИНХРОН: ничего не меняем,
      помечаем is_desync и логируем.
    - Момент синхронизации фиксирует updated_at.
    """
    result = get_all_reserved_parties()

    if result.get('is_error'):
        return {
            'is_error': True,
            'message': result.get('message_error', 'Ошибка получения данных из ЧЗ'),
            'created': 0, 'updated': 0, 'desync': 0, 'total': 0,
        }

    lst_party_info = result.get('lst_party_number_info', [])

    if not lst_party_info:
        return {
            'is_error': False,
            'message': 'В ЧЗ нет зарезервированных партий',
            'created': 0, 'updated': 0, 'desync': 0, 'total': 0,
        }

    # Статусы, нормальные для УИП, числящегося в резерве ЧЗ.
    NORMAL_STATUSES = [
        PartyStatusChoices.RESERVED_CZ,
        PartyStatusChoices.RESERVED_LOCAL,
    ]

    created_count = 0
    updated_count = 0
    desync_count = 0

    try:
        with transaction.atomic():
            for party_info in lst_party_info:
                party_number = party_info.get('partyNumber', '')

                if not party_number:
                    logger.warning(f'Пропущена запись без номера партии: {party_info}')
                    continue

                # === Дата резервирования ===
                created_dt = parse_cz_datetime(party_info.get('createdDateTime'))
                reservation_date = (
                    created_dt.date()
                    if created_dt
                    else None
                )
                if reservation_date is None:
                    logger.warning(
                        f'УИП {party_number}: отсутствует или некорректна дата резервирования '
                        f'(createdDateTime) в ответе ЧЗ.'
                    )

                # === Дата производства — предпочитаем явную из ЧЗ, иначе парсим из номера. ===
                production_date = parse_cz_date(party_info.get('productionDate'))
                if production_date is None and len(party_number) >= 20:
                    try:
                        date_str = party_number[14:20]
                        production_date = datetime.strptime(date_str, '%y%m%d').date()
                    except ValueError:
                        pass

                # === SKU ищем по GTIN (предпочитаем явный из ответа ЧЗ). ===
                gtin = party_info.get('gtin') or (
                    party_number[:14] if len(party_number) >= 14 else None
                )
                product_sku = None
                if gtin:
                    product_sku = ProductSKU.objects.filter(
                        product__packagings__gtin=gtin,
                        is_active=True
                    ).first()

                existing_uip = UIP.objects.filter(number=party_number).first()

                # === УИП НЕ НАЙДЕН — создаём ===
                if existing_uip is None:
                    uip = UIP.objects.create(
                        number=party_number,
                        product_sku=product_sku,
                        status=PartyStatusChoices.RESERVED_CZ,
                        production_date=production_date,
                        reservation_date=reservation_date,
                        planned_quantity=party_info.get('expectedQuantity', 0),
                        is_desync=False,
                        description='Создан при синхронизации с ЧЗ',
                    )
                    UIPStatusLog.objects.create(
                        uip=uip,
                        from_status=None,
                        to_status=PartyStatusChoices.RESERVED_CZ,
                        source='sync',
                        note='Создан при синхронизации с ЧЗ',
                    )
                    created_count += 1
                    logger.info(f'Создан УИП: {party_number} (резерв: {reservation_date})')
                    continue

                # === УИП НАЙДЕН — обновляем ===
                current_status = existing_uip.status

                if current_status in NORMAL_STATUSES:
                    # Норма: актуализируем данные, статус не меняем, снимаем флаг.
                    existing_uip.product_sku = product_sku or existing_uip.product_sku
                    existing_uip.production_date = production_date or existing_uip.production_date
                    existing_uip.planned_quantity = party_info.get('expectedQuantity', 0)
                    # reservation_date НЕ перезаписываем; заполняем только если он был пуст.
                    if existing_uip.reservation_date is None and reservation_date is not None:
                        existing_uip.reservation_date = reservation_date
                    existing_uip.is_desync = False
                    existing_uip.save()  # updated_at зафиксирует момент синхронизации.
                    logger.info(f'Обновлён УИП: {party_number} (статус: {current_status})')
                else:
                    # РАССИНХРОН: статус и данные НЕ меняем — только помечаем и логируем.
                    existing_uip.is_desync = True
                    existing_uip.save()
                    desync_count += 1
                    logger.warning(
                        f'РАССИНХРОН: УИП {party_number} имеет статус '
                        f'"{existing_uip.get_status_display()}", но числится '
                        f'зарезервированным в ЧЗ. Требуется проверка администратором.'
                    )

                updated_count += 1

        message = (
            f'Синхронизация завершена. Создано: {created_count}, '
            f'обновлено: {updated_count}'
        )
        if desync_count:
            message += f', ⚠ рассинхрон: {desync_count}'
        logger.info(message)

        return {
            'is_error': False,
            'message': message,
            'created': created_count,
            'updated': updated_count,
            'desync': desync_count,
            'total': len(lst_party_info),
        }

    except Exception as e:
        logger.error(f'Ошибка при синхронизации партий: {e}', exc_info=True)
        return {
            'is_error': True,
            'message': f'Ошибка при сохранении данных: {str(e)}',
            'created': created_count,
            'updated': updated_count,
            'desync': desync_count,
            'total': len(lst_party_info),
        }


def _generate_local_uip(
        article: str,
        gtin: str,
        production_date: date,
        is_external_service: bool,
        target_status: str = None,
        skip_cz: bool = False,
) -> dict:
    """
    Создаёт УИП в локальном согласованном формате И резервирует его в ЧЗ.

    Собственный источник Номера партии: (GTIN + дата + артикул + нули),
    но резервирование всё равно проходит на стороне Честного Знака.

    :param is_external_service: Возвращать номер без ошибки, если УИП уже существует.
    :param target_status: Переопределить статус создаваемого УИП.
                          Если None: DRAFT при skip_cz, иначе RESERVED_LOCAL.
    :param skip_cz: Если True — НЕ резервировать в ЧЗ (черновик для тестов).
    """
    number = build_local_party_number(gtin, production_date, article=article)
    print(f'Приходящие данные:\n'
          f'article: {article},\n'
          f'gtin: {gtin}\n'
          f'production_date: {production_date}\n'
          f'is_external_service: {is_external_service}\n'
          f'target_status: {target_status}\n'
          f'skip_cz: {skip_cz}')

    # Определяем целевой статус.
    if target_status is None:
        target_status = (
            PartyStatusChoices.DRAFT
            if skip_cz else PartyStatusChoices.RESERVED_LOCAL
        )

    if target_status not in PartyStatusChoices.values:
        return {
            'is_error': True,
            'message': f'Недопустимый статус: {target_status}'
        }

    # 1. Проверка на дубликат в локальной БД.
    try:
        uip = UIP.objects.get(number=number)
        if is_external_service:
            return {
                'is_error': False,
                'uuid_uip': uip.id,
                'reservation_date': uip.reservation_date,
                'status': uip.status,
                'number': number,
                'message': f'УИП с номером {number} уже существует.'
            }
        return {
            'is_error': True,
            'uuid_uip': uip.id,
            'reservation_date': uip.reservation_date,
            'status': uip.status,
            'number': number,
            'message': f'УИП с номером {number} уже существует.'
        }
    except ObjectDoesNotExist:
        logger.info(
            f'УИП с номером {number} не найден в локальной базе. '
            f'Будет произведена попытка генерации'
        )

    # Получить продукт по его артикулу.
    try:
        product_sku = ProductSKU.objects.get(article=article)
    except ObjectDoesNotExist:
        logger.error(f'Отсутствует продукт указанный в запросе: {article}')
        return {
            'is_error': True,
            'message': f'Проверьте артикул продукта или наличие продукта в базе СУП.'
        }

    # 2. Резервируем сформированный номер в Честном Знаке.
    if not skip_cz:
        reserve_result = reserve_parties_honest_sign(
            product_group=product_sku.product.group,
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

    # 3. Сохраняем УИП локально.
    source = 'test' if skip_cz else 'manual_local'
    note = (
        'Черновой УИП для тестирования (без резервирования в ЧЗ)'
        if skip_cz
        else 'Сгенерирован вручную (локальный формат), зарезервирован в ЧЗ'
    )

    try:
        with transaction.atomic():
            uip = UIP.objects.create(
                product_sku=product_sku,
                number=number,
                status=target_status,
                production_date=production_date,
                # Для черновика даты резервирования нет.
                reservation_date=(
                    None
                    if skip_cz
                    else timezone.now().date()
                ),
                description=note,
            )
            UIPStatusLog.objects.create(
                uip=uip,
                from_status=None,
                to_status=target_status,
                source=source,
                note=note,
            )
        logger.info(f'Создан УИП: {number} (статус: {target_status}, skip_cz: {skip_cz})')
        return {
            'is_error': False,
            'uuid_uip': uip.id,
            'reservation_date': uip.reservation_date,
            'number': number,
            'status': target_status,
            'message': f'УИП создан: {number} (статус: {target_status})',
        }
    except Exception as e:
        logger.error(f'Ошибка сохранения УИП: {e}', exc_info=True)
        return {'is_error': True, 'message': f'Ошибка сохранения УИП: {str(e)}'}


def _generate_cz_uip(
        sku: ProductSKU,
        gtin: str,
        production_date: date,
) -> dict:
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

                # Дата резервирования — из ответа ЧЗ (createdDateTime).
                created_dt = parse_cz_datetime(info.get('createdDateTime'))
                reservation_date = created_dt.date() if created_dt else timezone.now().date()

                existing_uip = UIP.objects.filter(number=number).first()

                if existing_uip is None:
                    # === Создаём новый УИП + лог установки начального статуса. ===
                    uip = UIP.objects.create(
                        number=number,
                        product_sku=sku,
                        status=PartyStatusChoices.RESERVED_CZ,
                        production_date=production_date,
                        reservation_date=reservation_date,
                        description='Сгенерирован вручную через Честный Знак',
                    )
                    UIPStatusLog.objects.create(
                        uip=uip,
                        from_status=None,
                        to_status=PartyStatusChoices.RESERVED_CZ,
                        source='manual_cz',
                        note='Сгенерирован вручную через Честный Знак',
                    )
                else:
                    # === Обновляем существующий. ===
                    existing_uip.product_sku = sku
                    existing_uip.production_date = production_date
                    # reservation_date не перезаписываем, заполняем только если пуст.
                    if existing_uip.reservation_date is None:
                        existing_uip.reservation_date = reservation_date
                    existing_uip.description = 'Сгенерирован вручную через Честный Знак'
                    existing_uip.save()
                    # Смена статуса через единый метод.
                    existing_uip.change_status(
                        PartyStatusChoices.RESERVED_CZ,
                        source='manual_cz',
                        note='Повторная генерация через Честный Знак',
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
            'number': (
                created[0]
                if len(created) == 1
                else ', '.join(created),
            ),
            'uuid_uip': uip.id,
            'message': f'Сгенерировано УИП через ЧЗ: {len(created)} шт.',
        }
    except Exception as e:
        logger.error(f'Ошибка сохранения УИП из ЧЗ: {e}', exc_info=True)
        return {
            'is_error': True,
            'message': f'Ошибка сохранения: {str(e)}'
        }


def generate_uip(
        product_sku: ProductSKU,
        production_date: date,
        mode: str,
        is_external_service: bool = False,
        target_status: str = None,
        skip_cz: bool = False,
) -> dict:
    """
    Точка входа генерации УИП внутри сервиса.
    :param product_sku: артикул продукта.
    :param production_date: дата маркировки (производства).
    :param mode: 'local' — локальный согласованный формат, 'cz' — через Честный Знак.
    :param is_external_service: запрос УИП из внешней системы.
    :param target_status: переопределить статус создаваемого УИП.
    :param skip_cz: не взаимодействовать с ЧЗ (черновик для тестов, только для local).
    """

    if not product_sku:
        logger.error(
            f"Критическая ошибка. Отсутствует запрошенный продукт: {product_sku}",
            exc_info=True
        )
        return {
            'is_error': True,
            'message': 'Продукт не найден или неактивен.'
        }

    article = product_sku.article
    gtin = product_sku.product.consumer_gtin

    if mode == 'local':
        return _generate_local_uip(
            article, gtin, production_date,
            is_external_service, target_status, skip_cz
        )
    if mode == 'cz':
        if skip_cz:
            return {
                'is_error': True,
                'message': 'Черновая генерация (skip_cz) доступна только в режиме local.'
            }
        return _generate_cz_uip(article, gtin, production_date)

    return {
        'is_error': True,
        'message': 'Неизвестный режим генерации.'
    }
