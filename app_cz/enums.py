# app_cz/enums.py

from enum import Enum


class Signature(Enum):
    CADES_BES = 1
    CADES_DEFAULT = 0
    CAPICOM_ENCODE_BASE64 = 0
    CAPICOM_CURRENT_USER_STORE = 2
    CAPICOM_MY_STORE = 'My'
    CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED = 2
