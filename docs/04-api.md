# 04. API (внешние методы)

Базовый адрес: `http://<host>:<port>/`. Префиксы: `/factory/` — справочники, `/uip/` — УИП,
`/cz/` — «Честный Знак» и внешний сервис, `/helper/` — служебные методы.
Интерактивная документация: `/api/docs/`, схема OpenAPI: `/api/schema/`.

Формат данных — JSON (UTF-8). Ошибки: `{"is_error": true, "message": "...", "errors": {...}}`
(часть методов — `has_error`).

Аутентификация: публичные методы доступны без входа; административные (помечены ниже) требуют
роли «Админ» (или суперпользователя/staff).

## Справочники (публичные)

| Метод | Назначение |
| --- | --- |
| `GET /factory/api/v1/dictionaries/workshops/` | Список цехов (`factory_id`, `is_active`) |
| `GET /factory/api/v1/dictionaries/lines/` | Список линий (`workshop_id`, `factory_id`, `is_active`) |
| `GET /factory/api/v1/dictionaries/products/` | Продукты (`group`, `is_active`, `search`, `article`, `gtin`) |

Справочники возвращают `{"result": [ ... ]}` без пагинации.

**Пример:** `GET /factory/api/v1/dictionaries/products/`

```json
{
  "result": [
    {
      "id": "87036430-004b-4aa0-bd64-641a9d197531",
      "name": "Творог Вкуснотеево 300г 9% стабилобэг",
      "articles": ["15613"],
      "gtin": "04601751016911",
      "gtin_packaging": "04601751016928",
      "gtin_pallets": null,
      "expiration_date": 24,
      "expiration_time": 0,
      "box_nesting": 6,
      "pallet_nesting": 100,
      "is_active": true,
      "code_storage_period_in_days": 60,
      "code_tnved": "0406105002"
    }
  ]
}
```

## УИП (публичные, кроме резервирования)

### Проверка статуса одного УИП
`GET /uip/api/v1/status_parties/{number}/` — 200, если УИП действующий; 404, если не найден
или в статусе `draft`/`deleted`.

```json
{
  "number": "04601751016843160622680816000000",
  "status": "reserved_cz",
  "status_display": "Зарезервирован в ЧЗ (сгенерирован)",
  "is_active": true,
  "detail": null
}
```

### Пакетная проверка
`POST /uip/api/v1/status_parties/batch/` (до 100 номеров). 200 — все найдены и действующие,
иначе 404.

```json
{ "numbers": ["04601751016843160622680816000000", "04601751016843160622680816000001"] }
```

```json
{
  "total": 2, "found": 2, "not_found": [],
  "details": [
    {"number": "04601751016843160622680816000000", "status": "reserved_cz",
     "status_display": "Зарезервирован в ЧЗ (сгенерирован)", "is_active": true, "detail": null}
  ]
}
```

### Список действующих УИП
`GET /uip/api/v1/status_parties/active/` — фильтры: `include_all_statuses`, `product_id`,
`product_article`, `gtin`, `search`, `production_date_from/to`, `reservation_date_from/to`.
По умолчанию: `reserved_cz`/`reserved_local` + `registered` за последние 7 дней.

### Единый поиск
`GET /uip/api/v1/search/?q=<запрос>&type=code|uip&page=&page_size=` — тип определяется
автоматически; управляющие символы DataMatrix (GS/FNC1) отбрасываются. Возвращает
`{query, search_type, count, page, page_size, results}`.

### Резервирование УИП (админ)
`POST /uip/api/v1/reserve-uips/` — один объект или массив. Два сценария:

- генерация: `article`/`gtin` + `production_date` [+ `count` (1..50), `mode` (`local`/`cz`),
  `party`, `target_status`, `skip_cz`];
- свои номера: `product_group` + `party_numbers` (1..50).

```json
[
  {"article": "50032", "production_date": "2026-09-21", "mode": "local", "count": 1},
  {"product_group": "milk", "party_numbers": ["04601751016843160622680816000001"]}
]
```

## Зарезервированные партии и коды

| Метод | Назначение |
| --- | --- |
| `GET /cz/api/v1/reserved_parties/` | Список УИП с фильтрами (пагинация) |
| `GET /cz/api/v1/reserved_parties/{id_or_number}/` | Детальная информация |
| `GET /cz/api/v1/reserved_parties/{id_or_number}/codes/` | Дерево кодов |
| `GET /cz/api/v1/codes/` | Список кодов (`search`, `ordering`) |
| `GET /cz/api/v1/codes/{code}/` | Данные о коде |
| `GET /cz/api/v1/parties/` | Производственные партии (`search`, `ordering`) |

Фильтры списка партий: `status` (через запятую или `all`), `marking_date` (dd.mm.yyyy),
`marking_date_from`, `marking_time` (HH:MM), `workshop_id`, `line_id`, `product_id`, `article`,
`search`, `ordering`, `page`.

**Данные о коде:** `GET /cz/api/v1/codes/{code}/`

```json
{
  "code": "01046017510218472150MrJl93tvlL",
  "level": "Потребительская (Штука)",
  "cz_status": "Нанесён (оплачен)",
  "production_status": "Нанесён",
  "uip_number": "04601751008091260720F5p.vyCjxSvO",
  "external_task_number": "TASK-2026-0015",
  "internal_party_number": "145",
  "factory_name": "ПАО МКВ",
  "workshop_name": "Цех розлива №1",
  "line_name": "Линия розлива 3",
  "gtin": "04601751021847",
  "product_name": "Молоко 3.2% 1л"
}
```

## Генерация и резервирование в ЧЗ (админ)

| Метод | Назначение |
| --- | --- |
| `POST /cz/api/generate-parties/` | Генерация номеров партий средствами ЧЗ |
| `POST /cz/api/reserve-parties/` | Резервирование своих номеров партий |
| `GET /cz/api/get-all-reserved-parties/` | Все зарезервированные партии из ЧЗ |
| `POST /cz/api/close-party-reservation/` | Снятие с резерва через отчёт о нанесении |
| `POST /cz/api/v1/generate-uip/` | Генерация одного УИП (внешние системы) |
| `POST /cz/api/reserve-draft-uip/` | Резервирование черновой УИП |

**Генерация номеров:** тело `{product_group, party_info_list: [{gtin, productionDate, count}]}`.

## Синхронизация с «Молвест.Маркировка»

| Метод | Назначение |
| --- | --- |
| `POST /cz/api/tasks/receive/` | Приём задания из внешнего сервиса |
| `POST /cz/api/tasks/sync-codes/` | Ручная синхронизация кодов задания (админ) |
| `POST /cz/api/codes/sync-task/` | Синхронизация кодов с явным URL/заданием (админ) |
| `POST /cz/sync/all/` | Полная ручная синхронизация (задания + коды, админ) |

**Приём задания:** обязателен `uuid_str`/`uuid_task`; остальные поля соответствуют модели Task
внешнего сервиса (`uip`, `uuid_uip`, `party`, `status`, `date_work`, `plan_amount`, `amount` и др.).

## Служебные

`GET /helper/api/v1/status_service/` — доступность сервиса, версия, нагрузка, проверки внешних
зависимостей.

> Полный перечень методов (включая административные и UI) — в Swagger: `/api/docs/`.
