# app_cz/services/national_catalog_client.py

# Клиент для работы с «Национальным каталогом» (раздел 10 «Методы "Национального
# каталога"» из True_API_GIS_MT.txt). Методы каталога размещаются на базовом
# адресе True API (/api/v3/true-api/nk/...) и требуют JWT-токена ГИС МТ.

import logging
import time
from typing import Dict, List, Optional

import requests

from app_factory.models import NationalCatalogProduct
from app_cz.models import SUZAccount
from app_cz.services.rate_limit import wait_nk_endpoint
from app_cz.services.true_api_client import TrueAPIClient
from app_event.utils import log_event

logger = logging.getLogger(__name__)

# Количество повторных попыток при HTTP 429 («Превышен лимит запросов»).
# Пауза между попытками растёт (30, 60, 120 секунд) — см. _make_request.
RATE_LIMIT_RETRIES = 3

# Максимальная глубина дробления батча при ошибке (25 → 12/13 → ... → 1).
# Нужно, чтобы один проблемный good_id не «ронял» весь батч из 25 товаров.
MAX_SPLIT_DEPTH = 5


def _is_rate_limit_response(response) -> bool:
    """True, если ответ — превышение лимита запросов.

    True API может вернуть 429 или 400 с текстом «лимит»/«Превышен» в теле.
    """
    if response.status_code == 429:
        return True
    if response.status_code != 400:
        return False
    try:
        body = (response.text or '')
    except Exception:
        return False
    text = body.lower()
    return (
        'лимит' in text
        or 'превышен' in text
        or 'много запросов' in text
    )


def _log_response_error(url: str, exc: Exception):
    """Логирует ошибку запроса вместе с телом ответа True API.

    Тело важно для диагностики: True API кладёт в него error_message /
    errorResult с причинами (например, какие good_id не найдены).
    """
    body = ''
    if getattr(exc, 'response', None) is not None:
        try:
            body = (exc.response.text or '')[:2000]
        except Exception:
            body = ''
    if body:
        logger.error(f'Ошибка запроса к Национальному каталогу: {url}, {exc}. Ответ: {body}')
    else:
        logger.error(f'Ошибка запроса к Национальному каталогу: {url}, {exc}')


def _extract_bad_good_ids(exc: Exception, good_ids: List[int]) -> List[int]:
    """Извлекает проблемные good_id из тела ошибки True API.

    True API при 400 для батча /nk/product обычно возвращает в теле
    errorResult/error_message с указанием конкретных good_id/gtin, которые
    не найдены или недоступны. Зная их, не нужно дробить батч пополам —
    достаточно повторить запрос один раз без проблемных ID.

    :return: Список good_id из good_ids, которые API пометил как ошибочные.
    """
    response = getattr(exc, 'response', None)
    if response is None:
        return []
    try:
        payload = response.json()
    except Exception:
        payload = {}

    bad = set()
    if isinstance(payload, dict):
        # errorResult / error_message — типовые поля True API.
        err_result = payload.get('errorResult') or payload.get('error_result')
        error_message = str(
            payload.get('errorMessage') or payload.get('error_message') or ''
        )
        # Собираем все числа из errorResult (good_id/gtin/...).
        def _collect_numbers(node):
            if isinstance(node, list):
                for item in node:
                    _collect_numbers(item)
            elif isinstance(node, dict):
                for value in node.values():
                    _collect_numbers(value)
            elif isinstance(node, (int, str)):
                text = str(node).strip()
                if text.lstrip('-').isdigit():
                    bad.add(int(text))

        _collect_numbers(err_result)

        # В error_message могут упоминаться ID прямо в тексте.
        for gid in good_ids:
            if str(gid) in error_message:
                bad.add(gid)

    return [gid for gid in good_ids if gid in bad]

# Размер батча для «Метод получения информации о товаре» (/nk/product).
# Ограничение метода: не более 25 «good_id» в одном запросе (True_API_GIS_MT.txt).
PRODUCT_BATCH_SIZE = 25

# «Справочник «Список поддерживаемых товарных групп»» (True_API_GIS_MT.txt).
# Ключ — «Код в БД» товарной группы, возвращаемый в поле gismt_codes категории
# Национального каталога; значение — (код товарной группы, наименование).
GISMST_PRODUCT_GROUPS = {
    1: ('lp', 'Лёгкая промышленность'),
    2: ('shoes', 'Обувные товары'),
    3: ('tobacco', 'Табачная продукция'),
    4: ('perfumery', 'Духи и туалетная вода'),
    5: ('tires', 'Шины и покрышки'),
    6: ('electronics', 'Фотокамеры'),
    8: ('milk', 'Молочная продукция'),
    9: ('bicycle', 'Велосипеды'),
    10: ('wheelchairs', 'Медицинские изделия'),
    11: ('alcohol', 'Алкоголь'),
    12: ('otp', 'Альтернативная табачная продукция'),
    13: ('water', 'Упакованная вода'),
    14: ('furs', 'Товары из натурального меха'),
    15: ('beer', 'Пиво и слабоалкогольные напитки'),
    16: ('ncp', 'Никотиносодержащая продукция'),
    17: ('bio', 'Специализированная пищевая продукция и БАД к пище'),
    19: ('antiseptic', 'Антисептики и дезинфицирующие средства'),
    20: ('petfood', 'Корма для животных'),
    21: ('seafood', 'Морепродукты'),
    22: ('nabeer', 'Безалкогольное пиво'),
    23: ('softdrinks', 'Соковая продукция и безалкогольные напитки'),
    25: ('meat', 'Мясные изделия'),
    26: ('vetpharma', 'Ветеринарные препараты'),
    27: ('toys', 'Игры и игрушки для детей'),
    28: ('radio', 'Радиоэлектронная продукция'),
    31: ('titan', 'Титановая металлопродукция'),
    32: ('conserve', 'Консервированная продукция'),
    33: ('vegetableoil', 'Растительные масла'),
    34: ('opticfiber', 'Оптоволокно и оптоволоконная продукция'),
    35: ('chemistry', 'Косметика, бытовая химия и товары личной гигиены'),
    36: ('books', 'Печатная продукция'),
    37: ('grocery', 'Бакалейная продукция'),
    38: ('pharmaraw', 'Фармацевтическое сырьё, лекарственные средства'),
    39: ('construction', 'Строительные материалы'),
    40: ('fire', 'Пиротехника и огнетушащее оборудование'),
    41: ('heater', 'Отопительные приборы'),
    42: ('cableraw', 'Кабельно-проводниковая продукция'),
    43: ('autofluids', 'Моторные масла'),
    44: ('polymer', 'Полимерные трубы'),
    45: ('sweets', 'Сладости и кондитерские изделия'),
    48: ('carparts', 'Автозапчасти и комплектующие транспортных средств'),
    49: ('furslp', 'Натуральный мех'),
    50: ('nicotindev', 'Радиоэлектронная продукция. ЭСДН'),
    51: ('gadgets', 'Радиоэлектронная продукция. Ноутбуки и смартфоны'),
    52: ('frozen', 'Полуфабрикаты и замороженные продукты'),
    53: ('fertilizers', 'Удобрения в потребительской упаковке'),
    54: ('homeware', 'Товары для дома и интерьера'),
    59: ('pyrotechnics', 'Пиротехнические изделия'),
}


def _extract_gtin(item: Dict) -> str:
    """Извлекает GTIN потребительской упаковки (trade-unit).

    В ответе НК GTIN-13 приходит без ведущего нуля. Для работы с ЧЗ требуется
    14-значный GTIN-14, поэтому значение дополняется ведущими нулями.
    """
    trade_unit_value = ''
    for identified in item.get('identified_by') or []:
        if identified.get('type') != 'gtin':
            continue
        value = str(identified.get('value', '') or '').strip()
        if not value:
            continue
        # Отдаём предпочтение уровню trade-unit (потребительская упаковка).
        if identified.get('level') == 'trade-unit':
            trade_unit_value = value
            break
        if not trade_unit_value:
            trade_unit_value = value
    return _normalize_gtin(trade_unit_value)


def _normalize_gtin(value: str) -> str:
    """Приводит GTIN к 14-значному формату GTIN-14.

    GTIN-13 (EAN-13) и более короткие коды дополняются ведущими нулями
    до 14 знаков (как требует Честный ЗНАК).
    """
    value = str(value or '').strip()
    if value.isdigit() and len(value) < 14:
        return value.zfill(14)
    return value


def _extract_image_url(item: Dict) -> str:
    """Извлекает ссылку на первое изображение товара."""
    for image in item.get('good_images') or []:
        url = image.get('photo_url')
        if url:
            return url
    return ''


def _extract_product_group(
    item: Dict, categories_by_id: Dict[int, List[int]]
) -> tuple:
    """Определяет товарную группу товара по кодам категорий каталога."""
    codes: List[int] = []
    for category in item.get('categories') or []:
        cat_id = category.get('cat_id')
        if cat_id is not None:
            for gismt_code in categories_by_id.get(int(cat_id), []):
                if gismt_code not in codes:
                    codes.append(gismt_code)

    for gismt_code in codes:
        group = GISMST_PRODUCT_GROUPS.get(gismt_code)
        if group:
            return group
    return '', ''


class NationalCatalogClient:
    """Клиент «Национального каталога» (ГИС МТ).

    Методы каталога размещаются на базовом адресе True API и аутентифицируются
    JWT-токеном (Bearer), получаемым через TrueAPIClient.get_jwt_token().
    """

    PAGE_SIZE = 100

    def __init__(self, user, progress: Optional[Dict] = None):
        self.user = user
        # Словарь прогресса (при передаче — обновляется при ожидании лимита).
        self.progress = progress
        self.true_api_client = TrueAPIClient(user=user)
        self.base_url = self.true_api_client.base_url

    def _on_rate_wait(self, delay: float):
        """Обновляет прогресс, когда запрос отложен из-за лимита ЧЗ."""
        if self.progress is None:
            return
        if not self.progress.get('waiting'):
            # Запоминаем фазу, чтобы вернуть её после ожидания.
            self._wait_prev_phase = self.progress.get('phase', 'products')
        self.progress['waiting'] = True
        self.progress['phase'] = 'rate_wait'
        self.progress['current_name'] = (
            f'Ожидание лимита запросов Честного Знака... (~{int(delay)} сек)'
        )

    def _clear_rate_wait(self):
        """Снимает признак ожидания лимита и возвращает фазу."""
        if self.progress is None:
            return
        self.progress['waiting'] = False
        if getattr(self, '_wait_prev_phase', None):
            self.progress['phase'] = self._wait_prev_phase
            self._wait_prev_phase = None

    def _auth_headers(self) -> Dict[str, str]:
        if not getattr(self, '_jwt_token', None):
            self._jwt_token = self.true_api_client.get_token()
        return {
            'accept': 'application/json',
            'Authorization': f'Bearer {self._jwt_token}',
        }

    def _make_request(
        self, method: str, endpoint: str, **kwargs
    ) -> Dict:
        url = f'{self.base_url}{endpoint}'
        headers = kwargs.pop('headers', {})
        headers = {**self._auth_headers(), **headers}

        # Соблюдаем документированные лимиты True API (True_API_GIS_MT.txt):
        # /nk/product — не более 10 запросов за 5 минут, остальные методы НК —
        # не более 10 запросов в секунду. Ожидание блокирует поток до слота.
        wait_nk_endpoint(endpoint, on_wait=self._on_rate_wait)
        self._clear_rate_wait()

        last_error = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = requests.request(
                    method, url, timeout=30, headers=headers, **kwargs
                )
                # Лимит запросов: True API может вернуть 429 или 400 с текстом
                # «лимит»/«Превышен» в теле. Повторяем с нарастающей паузой.
                if _is_rate_limit_response(response):
                    backoff = 30 * (2 ** attempt)
                    logger.warning(
                        f'True API вернул {response.status_code} (лимит запросов) '
                        f'для {url} '
                        f'(попытка {attempt + 1}/{RATE_LIMIT_RETRIES + 1}), '
                        f'пауза {backoff} сек'
                    )
                    time.sleep(backoff)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.exceptions.HTTPError as e:
                # Другие ошибки 4xx/5xx не являются лимитом — повторяем только
                # 429/400 с признаком лимита в теле.
                if e.response is not None and _is_rate_limit_response(e.response):
                    last_error = e
                    continue
                _log_response_error(url, e)
                raise
            except requests.exceptions.RequestException as e:
                _log_response_error(url, e)
                raise

        logger.error(
            f'Превышен лимит запросов к Национальному каталогу: {url} '
            f'после {RATE_LIMIT_RETRIES} повторных попыток'
        )
        raise last_error

    def get_etags_list(
        self,
        offset: int = 0,
        owner_inn: Optional[str] = None,
        brand_id: Optional[str] = None,
        cat_id: Optional[int] = None,
    ) -> Dict:
        """Список товаров владельца и их хешей (не более 100 записей за запрос).

        URL: /nk/etagslist
        """
        params = {'format': 'json', 'offset': offset}
        if owner_inn:
            params['owner_inn'] = owner_inn
        if brand_id:
            params['brand_id'] = brand_id
        if cat_id is not None:
            params['cat_id'] = cat_id
        return self._make_request('GET', '/nk/etagslist', params=params)

    def get_all_products(
        self,
        owner_inn: Optional[str] = None,
        brand_id: Optional[str] = None,
        cat_id: Optional[int] = None,
        progress: Optional[Dict] = None,
    ) -> List[Dict]:
        """Получение ВСЕХ товаров владельца постранично."""
        products: List[Dict] = []
        offset = 0
        total = 0

        while True:
            data = self.get_etags_list(
                offset=offset,
                owner_inn=owner_inn,
                brand_id=brand_id,
                cat_id=cat_id,
            )
            result = data.get('result') or {}
            goods = result.get('goods') or []
            products.extend(goods)

            total = result.get('total', 0)
            last_product_number = result.get(
                'last_product_number', offset + len(goods)
            )

            if progress is not None:
                progress['phase'] = 'list'
                progress['total'] = total
                progress['done'] = len(products)
                progress['current_name'] = f'Загрузка списка товаров... ({len(products)}/{total})'

            if (
                not goods
                or last_product_number >= total
                or len(products) >= total
            ):
                break
            offset = last_product_number

        return products

    def get_products(
        self,
        good_ids: Optional[List[int]] = None,
        gtins: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Информация о товарах по списку идентификаторов (батч).

        URL: /nk/product
        """
        params = {'format': 'json'}
        if good_ids:
            params['good_ids'] = ';'.join(
                str(good_id) for good_id in good_ids
            )
        elif gtins:
            params['gtins'] = ';'.join(gtins)
        else:
            raise ValueError('Укажите good_ids или gtins')

        data = self._make_request('GET', '/nk/product', params=params)
        return data.get('result') or []

    def fetch_products_resilient(
        self,
        good_ids: List[int],
        depth: int = 0,
        known_bad: Optional[set] = None,
    ):
        """Безопасно получает товары по списку good_id.

        True API возвращает 400 для ВСЕГО батча, если хотя бы один good_id
        проблемный (удалён, чужой, набор и т.п.). При ошибке:

        1. Пытаемся вытащить проблемные good_id из тела ошибки (errorResult)
           — тогда повторяем запрос один раз без них (быстро);
        2. Если в теле ошибки ID нет — дробится батч пополам рекурсивно,
           чтобы изолировать проблемные ID.

        Уже обнаруженные «плохие» good_id (known_bad) пропускаются сразу,
        чтобы не запрашивать их повторно в следующих батчах.

        :return: (items, failed_good_ids)
        """
        if not good_ids:
            return [], []
        known_bad = known_bad if known_bad is not None else set()

        # Пропускаем уже известные проблемные ID.
        pending = [g for g in good_ids if g not in known_bad]
        if not pending:
            return [], list(good_ids)

        try:
            return self.get_products(good_ids=pending), []
        except requests.exceptions.RequestException as e:
            # 1) Пробуем вытащить «плохие» good_id из тела ошибки.
            bad = _extract_bad_good_ids(e, pending)
            if bad:
                known_bad.update(bad)
                rest = [g for g in pending if g not in set(bad)]
                if not rest:
                    return [], bad
                try:
                    items = self.get_products(good_ids=rest)
                    return items, bad
                except requests.exceptions.RequestException:
                    # Остаток снова упал — дробим его.
                    pending = rest
                    bad_set = set(bad)
            else:
                bad_set = set()

            # 2) Дробим пополам.
            if depth >= MAX_SPLIT_DEPTH or len(pending) <= 1:
                return [], list(pending) + [g for g in bad if g in good_ids]

            mid = len(pending) // 2
            items = []
            failed = [g for g in bad if g in good_ids]
            for half in (pending[:mid], pending[mid:]):
                half_items, half_failed = self.fetch_products_resilient(
                    half, depth=depth + 1, known_bad=known_bad
                )
                items.extend(half_items)
                failed.extend(half_failed)
            return items, failed

    def get_product(
        self,
        good_id: Optional[int] = None,
        gtin: Optional[str] = None,
    ) -> List[Dict]:
        """Полная информация о товаре.

        URL: /nk/product
        """
        params = {'format': 'json'}
        if good_id is not None:
            params['good_id'] = good_id
        elif gtin:
            params['gtin'] = gtin
        else:
            raise ValueError('Укажите good_id или gtin')

        data = self._make_request('GET', '/nk/product', params=params)
        return data.get('result') or []

    def get_categories(
        self,
        cat_id: Optional[int] = None,
        gismt_code: Optional[int] = None,
        tnved: Optional[int] = None,
    ) -> List[Dict]:
        """Дерево категорий товаров.

        URL: /nk/categories
        """
        params = {'format': 'json'}
        if cat_id is not None:
            params['cat_id'] = cat_id
        elif gismt_code is not None:
            params['gismt_code'] = gismt_code
        elif tnved is not None:
            params['tnved'] = tnved

        data = self._make_request('GET', '/nk/categories', params=params)
        return data.get('result') or []

    def get_brands(
        self, name: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[Dict]:
        """Список товарных знаков.

        URL: /nk/brands
        """
        params = {'format': 'json', 'limit': limit, 'offset': offset}
        if name:
            params['name'] = name

        data = self._make_request('GET', '/nk/brands', params=params)
        return data.get('result') or []


def _categories_by_id(client: NationalCatalogClient) -> Dict[int, List[int]]:
    """Строит словарь {cat_id: [gismt_codes]} из дерева категорий каталога."""
    mapping: Dict[int, List[int]] = {}
    for category in client.get_categories():
        cat_id = category.get('cat_id')
        if cat_id is None:
            continue
        mapping[int(cat_id)] = [
            int(code) for code in (category.get('gismt_codes') or [])
        ]
    return mapping


def _extract_card_state(item: Dict) -> str:
    """Извлекает состояние карточки из ответа /nk/product."""
    status = item.get('good_status', '')
    mapping = {
        'draft': 'draft',
        'moderation': 'on_moderation',
        'errors': 'requires_moderation',
        'notsigned': 'awaiting_signature',
        'published': 'published',
        'archived': 'in_archive',
    }
    return mapping.get(status, '')


def _extract_state_condition(item: Dict) -> str:
    """Извлекает состояние товара по флагам готовности."""
    if item.get('good_turn_flag'):
        return 'ready_commercialization'
    if item.get('good_mark_flag'):
        return 'ready_order_km'
    return 'not_ready_order_km'


def _extract_create_date(item: Dict) -> str:
    """Извлекает дату создания карточки в НК из raw_data."""
    from datetime import datetime

    raw = item.get('create_date') or item.get('flag_updated_date') or item.get('update_date')
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def upsert_product(
    item: Dict,
    etag: str = '',
    categories_by_id: Optional[Dict[int, List[int]]] = None,
) -> tuple:
    """Создаёт или обновляет запись NationalCatalogProduct по карточке товара."""
    good_id = item.get('good_id')
    if good_id is None:
        raise ValueError('В карточке товара отсутствует good_id')

    product_group, product_group_name = _extract_product_group(
        item, categories_by_id or {}
    )
    defaults = {
        'etag': etag,
        'gtin': _extract_gtin(item),
        'name': item.get('good_name', ''),
        'categories': item.get('categories', []),
        'image_url': _extract_image_url(item),
        'product_group': product_group,
        'product_group_name': product_group_name,
        'card_state': _extract_card_state(item),
        'state_condition': _extract_state_condition(item),
        'create_date': _extract_create_date(item),
        'raw_data': item,
    }
    return NationalCatalogProduct.objects.update_or_create(
        good_id=good_id, defaults=defaults
    )


def sync_products(
    user,
    owner_inn: Optional[str] = None,
    brand_id: Optional[str] = None,
    cat_id: Optional[int] = None,
    progress: Optional[Dict] = None,
) -> Dict[str, int]:
    """Полная выгрузка товаров владельца из «Национального каталога» в БД.

    1. Собирает список всех товаров (good_id + etag) через /nk/etagslist.
    2. Один раз загружает дерево категорий для определения товарных групп.
    3. Батчами получает информацию о товарах через /nk/product.
    4. Создаёт/обновляет записи NationalCatalogProduct.

    Если owner_inn не передан, по умолчанию используется ИНН активной
    учётной записи СУЗ (SUZAccount.is_active=True).

    Если передан словарь progress, он обновляется по ходу синхронизации:
    {'total', 'done', 'created', 'updated', 'current_name'}.

    Возвращает статистику: {'total', 'created', 'updated'}.
    """
    if not owner_inn:
        account = SUZAccount.objects.filter(is_active=True).first()
        owner_inn = account.inn if account else None
    client = NationalCatalogClient(user, progress=progress)
    etag_products = client.get_all_products(
        owner_inn=owner_inn, brand_id=brand_id, cat_id=cat_id,
        progress=progress,
    )
    etags = {
        int(product['good_id']): product.get('etag', '')
        for product in etag_products
    }
    categories_by_id = _categories_by_id(client)

    if progress is not None:
        progress['total'] = len(etag_products)
        progress['done'] = 0
        progress['created'] = 0
        progress['updated'] = 0
        progress['phase'] = 'products'

    created = updated = 0
    done = 0
    errors = 0
    # Найденные проблемные good_id — пропускаются во всех следующих батчах,
    # чтобы не запрашивать их повторно (True API 400-ит весь батч из-за них).
    known_bad: set = set()
    for start in range(0, len(etag_products), PRODUCT_BATCH_SIZE):
        batch = etag_products[start : start + PRODUCT_BATCH_SIZE]
        good_ids = [int(product['good_id']) for product in batch]
        # При ошибке батч дробится: один проблемный good_id не «роняет» все 25.
        items, failed_ids = client.fetch_products_resilient(
            good_ids, known_bad=known_bad
        )
        if failed_ids:
            known_bad.update(failed_ids)
            errors += len(failed_ids)
            logger.error(
                'Не удалось получить товары НК (после дробления батча): %s',
                failed_ids,
            )

        for item in items:
            good_id = int(item.get('good_id'))
            try:
                _, is_created = upsert_product(
                    item,
                    etag=etags.get(good_id, ''),
                    categories_by_id=categories_by_id,
                )
                if is_created:
                    created += 1
                else:
                    updated += 1
            except Exception:
                errors += 1
                logger.exception('Ошибка сохранения товара НК good_id=%s', good_id)

        # Учитываем все попытки батча: успешные + отброшенные при дроблении.
        done += len(batch)
        if progress is not None:
            progress['done'] = done
            progress['created'] = created
            progress['updated'] = updated
            progress['errors'] = errors
            if items:
                progress['current_name'] = items[-1].get('good_name', '')

    log_event(
        module='nk',
        level='success' if created else 'info',
        message=(
            f'Синхронизация Национального каталога: '
            f'создано {created}, обновлено {updated}, '
            f'всего {len(etag_products)}, ошибок {errors}'
        ),
        actor=user.username,
    )
    return {
        'total': len(etag_products),
        'created': created,
        'updated': updated,
        'errors': errors,
    }
