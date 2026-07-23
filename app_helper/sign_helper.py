# app_helper/sign_helper.py

import logging
from datetime import datetime

from app_cz.enums import Signature

logger = logging.getLogger(__name__)

# Импорт библиотек для работы с ЭЦП
try:
    import pythoncom
    import win32com.client
except ImportError as e:
    logger.error(f"Не удалось импортировать библиотеки для работы с ЭЦП: {e}")


def _parse_certificate_string(cert_string):
    # Разделяем строку по запятым.
    parts = cert_string.split(',')

    # Создаем пустой словарь
    result = {}

    for part in parts:
        # Разделяем каждую часть по знаку '='.
        if '=' in part:
            key, value = part.split('=', 1)  # Разделяем только по первому '='.
            result[key.strip()] = value.strip().strip('"')  # Убираем лишние пробелы и кавычки.

    return result


def get_list_certificates() -> list:
    """Получает список валидных сертификатов из хранилища Windows."""

    list_certificates = []
    pythoncom.CoInitialize()

    try:
        oStore = win32com.client.Dispatch("CAdESCOM.Store")

        # 2 = CAPICOM_CURRENT_USER_STORE, "My" = CAPICOM_MY_STORE, 0 = CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED
        oStore.Open(
            Signature.CAPICOM_CURRENT_USER_STORE.value,
            Signature.CAPICOM_MY_STORE.value,
            Signature.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED.value,
        )

        for val in oStore.Certificates:
            str_serial_number = val.SerialNumber
            # Парсинг FIO (CN=...)
            subject_parts = val.SubjectName.split(', ')
            fio = subject_parts[0][3:] if subject_parts[0].startswith("CN=") else val.SubjectName
            # Парсинг ИНН (ИНН=...)
            lst_inn = [i[4:] for i in subject_parts if i.startswith("ИНН=")]
            inn = lst_inn[0] if lst_inn else '000000000000'

            str_valid_from = val.ValidFromDate.strftime("%d-%m-%Y")
            str_valid_for = val.ValidToDate.strftime("%d-%m-%Y")

            # Расчет оставшихся дней.
            valid_days = (datetime.strptime(str_valid_for, "%d-%m-%Y") - datetime.now()).days

            if valid_days >= 1 and val.IsValid():
                list_certificates.append({
                    'serial_number': str_serial_number,
                    'fio': fio,
                    'inn': inn,
                    'valid_from': str_valid_from.strftime("%d-%m-%Y") if hasattr(str_valid_from, 'strftime') else str(
                        str_valid_from),
                    'valid_for': str_valid_for.strftime("%d-%m-%Y") if hasattr(str_valid_for, 'strftime') else str(
                        str_valid_for),
                    'valid_days': valid_days
                })

        logger.info(f"Успешно найдено {len(list_certificates)} валидных сертификатов.")
        return list_certificates

    except Exception as e:
        # ВАЖНО: Логируем ошибку вместо молчаливого возврата []
        logger.error(f"Ошибка при чтении хранилища сертификатов: {e}", exc_info=True)
        return []

    finally:
        if 'oStore' in locals() and oStore:
            try:
                oStore.Close()
            except:
                pass
        pythoncom.CoUninitialize()
