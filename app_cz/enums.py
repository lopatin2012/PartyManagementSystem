# app_cz/enums.py

from datetime import date
from enum import Enum


# С этой даты УИП с коротким сроком годности обязательно резервируются в ЧЗ
# (а не остаются черновиками). Общая константа: используется накоплением
# резерва и генерацией УИП.
SHORT_SHELF_LIFE_RESERVE_FROM = date(2026, 11, 1)


class Signature(Enum):
    CADES_BES = 1
    CADES_DEFAULT = 0
    CAPICOM_ENCODE_BASE64 = 0
    CAPICOM_CURRENT_USER_STORE = 2
    CAPICOM_MY_STORE = 'My'
    CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2


class TypeProduct(Enum):
    MILK = 'milk'
    # Молоко.


class TemplateId(Enum):
    MILK = 20
    # Молоко.


class UsedInProduction(Enum):
    """
    Признак использования КМ на производстве.
    """
    not_were_used_to_produce = 0
    # Значение по умолчанию.

    were_used_to_produce = 1
    # Были использованы на производстве.
