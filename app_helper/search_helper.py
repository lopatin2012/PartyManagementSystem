# app_helper/search_helper.py

import re

from django.db.models import Q


def detect_search_type(query: str) -> str:
    """
    Автоматически определяет тип поискового запроса.

    Правила:
    - DataMatrix: начинается с AI '01' (GTIN), содержит AI '21' (серийный номер).
      Может содержать спец-символ GS (\x1D) как разделитель.
      Пример: 01046017510218472150MrJl93tvlL
              или 0104601751021847\x1D2150MrJl93tvlL

    - УИП: 14 цифр GTIN + 6 цифр даты (ГГММДД) + 1-12 символов серийника.
      Общая длина: 21-32 символа.
      Допустимые символы: цифры, латинские буквы, /. , -
      Пример: 04601751008091260720F5p.vyCjxSvO

    Возвращает: 'code' или 'uip'
    """
    query = query.strip()

    if not query:
        return 'code'

    # ==========================================
    # Проверка 1: DataMatrix (код маркировки)
    # ==========================================
    # DataMatrix начинается с AI '01' (GTIN)
    # После 14 цифр GTIN идёт либо AI '21' (серийный номер), либо спец-символ GS + '21'
    datamatrix_pattern = r'^01\d{14}(\x1D)?21.+'
    if re.match(datamatrix_pattern, query):
        return 'code'

    # Дополнительная проверка: если есть спец-символы (GS, FNC1 и т.д.) — это точно DataMatrix
    has_control_chars = any(ord(c) < 32 for c in query)
    if has_control_chars:
        return 'code'

    # ==========================================
    # Проверка 2: УИП (номер партии)
    # ==========================================
    # Формат: 14 цифр + 6 цифр даты + 1-12 символов серийника
    # Общая длина: 21-32 символа
    # Допустимые символы: цифры, латинские буквы, /. , -
    uip_pattern = r'^[0-9]{14}[0-9]{6}[A-Za-z0-9/.,\-]{1,12}$'

    if re.match(uip_pattern, query):
        # Дополнительная проверка длины (21-32 символа)
        if 21 <= len(query) <= 32:
            return 'uip'

    # ==========================================
    # По умолчанию: считаем DataMatrix кодом
    # ==========================================
    # Это самый частый сценарий использования (сканер штрих-кодов)
    return 'code'


def clean_datamatrix_code(code: str) -> str:
    """
    Очищает DataMatrix код от спец-символов (GS, FNC1 и т.д.).

    В БД коды хранятся в "чистом" виде, без управляющих символов.
    Пример:
    - Вход: 0104601751021847\x1D2150MrJl93tvlL
    - Выход: 01046017510218472150MrJl93tvlL
    """
    # Удаляем все непечатаемые символы (ASCII < 32)
    # Это включает GS (\x1D), FNC1 и другие управляющие символы
    return ''.join(c for c in code if ord(c) >= 32)


def filter_codes_by_query(queryset, query: str):
    """
    Поиск кодов маркировки: сначала ТОЧНОЕ совпадение, затем — по началу строки.

    Коды в БД могут содержать управляющий символ-разделитель GS (\\x1d) перед
    AI 93, поэтому проверяем и «сырой» запрос (как отдаёт сканер), и очищенный
    (без GS). Точное совпадение использует уникальный индекс `code` и работает
    за миллисекунды даже на миллионах строк; `iexact`/`istartswith` обычный
    индекс не используют (в SQL получается UPPER(code) LIKE ...), поэтому
    префиксный поиск идёт только как запасной вариант — по функциональному
    индексу `cis_code_upper_prefix_idx`.
    """
    cleaned = clean_datamatrix_code(query)

    candidates = [query]
    if cleaned and cleaned != query:
        candidates.append(cleaned)

    # 1) Точное совпадение (уникальный индекс).
    for candidate in candidates:
        exact = queryset.filter(code__exact=candidate)
        if exact.exists():
            return exact

    # 2) Запасной вариант — по началу строки.
    prefix = Q()
    for candidate in candidates:
        prefix |= Q(code__istartswith=candidate)
    return queryset.filter(prefix)


def filter_uips_by_query(queryset, query: str):
    """
    Поиск УИП: сначала ТОЧНОЕ совпадение номера (по индексу), затем — без учёта
    регистра (запасной вариант).
    """
    exact = queryset.filter(number__exact=query)
    if exact.exists():
        return exact

    return queryset.filter(number__iexact=query)
