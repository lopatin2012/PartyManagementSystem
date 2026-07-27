# app_cz/enums.py

from enum import Enum


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
