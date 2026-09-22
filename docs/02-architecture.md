# 02. Архитектура

## Общая схема

```
План производства / MES    ──┐                          ┌── «Молвест.Маркировка» (серверы заводов)
1С (ERP / УХ / УПП)        ──┤   PartyManagementSystem  ├── СУЗ / TrueAPI «Честный Знак»
WMS (задел)                ──┘        (монолит)          └── Национальный каталог (ГИС МТ)
```

Монолитное Django-приложение с разделением на бизнес-модули (приложения). Разворачивается в
Docker Compose (web + scheduler + worker + PostgreSQL).

## Приложения

| Приложение | Назначение |
| --- | --- |
| `app_factory` | Справочники: заводы, цеха, линии, продукты, упаковки (GTIN), SKU, товары Нац. каталога |
| `app_uip` | Ядро: `UIP`, `ProductionParty`, `UIPStatusLog`. Все смены статуса — через `UIP.change_status()` |
| `app_cz` | Интеграция с «Честным Знаком»: коды (`CISCode`), СУЗ-аккаунты, архив, сервисы |
| `app_scheduler` | Периодические задачи и планировщик (`run_scheduler`, `run_tasks_worker`) |
| `app_page` | UI-страницы: главная, поиск, список УИП |
| `app_helper` | Общее: `UUIDModel`, доступ/роли, поиск, подписи, пользователи, трекинг нагрузки |
| `app_wms` | Модуль склада (заготовка) |
| `app_event` | Лента событий: `EventLog` + `log_event()` |
| `config` | Настройки + приложение: модель `ExternalService` (адреса внешних сервисов) |

## Ключевые модели

### `app_uip`
- **`UIP`** — уникальный идентификатор партии. Номер: 14 цифр GTIN + 6 цифр даты (ГГММДД) +
  1–12 символов серийника. Поля: `status`, `is_desync`, `planned_quantity`, `produced_quantity`,
  `production_date`, `reservation_date`, даты аудита.
- **`ProductionParty`** — производственная партия (задание на линии): `external_number_task`
  (номер во внешней системе), `production_party` (внутренний номер), `line`, `status`,
  `sync_status`, `last_sync_*`, даты и количества.
- **`UIPStatusLog`** — аудит переходов статуса с источником (`admin`/`sync`/`api`/`service`).

### `app_cz`
- **`CISCode`** — код маркировки DataMatrix: `code`, `level` (L1/L2/L3), `cz_status`,
  `production_status`, `parent` (иерархия), связь с `ProductionParty` и `ProductPackaging`.
- **`CISCodeArchive`** — архив кодов (маршрутизируется в БД `archive`).
- **`SUZAccount`** — учётная запись СУЗ: сертификат, ИНН, токены.
- **`NKSyncState`** — синглтон-блокировка и прогресс синхронизации Нац. каталога.

### `app_factory`
- **`Factory`** — завод: `ip_address`/`port_address` (адрес локального сервера маркировки),
  поля состояния синхронизации заданий (`external_sync_changed_since` и др.).
- **`Workshop`**, **`Line`** — цех и линия.
- **`Product`**, **`ProductPackaging`** (GTIN + уровень), **`ProductSKU`** (артикул,
  `type_formation_uip`), **`NationalCatalogProduct`** (кэш Нац. каталога).

### `config`
- **`ExternalService`** — адреса внешних сервисов (подпись/WMS/печать, СУП и т.д.).

## Слои и паттерны

- **Сервисный слой** (`services/`): `app_cz`, `app_factory`, `app_uip`.
  Примеры: `app_uip/services/uip_reserve.py`, `app_cz/services/party_service.py`,
  `app_cz/services/code_sync.py`, `app_factory/services/nk_sync_service.py`.
- **Представления/сериализаторы**: UI-страницы в `app_page` и `app_cz/views.py`;
  DRF — ViewSet'ы и APIView.
- **`UUIDModel`** (`app_helper.models`) — абстрактная модель с UUIDv7-первичным ключом.
  Исключение: журнальные таблицы (`app_event.EventLog`) используют обычный `models.Model`.

## Две базы данных

- `default` — рабочая БД.
- `archive` — архивная БД для `app_cz.CISCodeArchive` (маршрутизация через `app_cz/routers.py`,
  денормализованный снимок без FK). Переменные `DB_ARCHIVE_*` по умолчанию указывают на ту же БД.

## Фоновые задачи и планировщик

- Очереди: `default`, `emails`, `high-priority` (django-tasks-db).
- Функции задач — `app_scheduler/tasks.py`.
- Расписание — `SCHEDULE` в `app_scheduler/management/commands/run_scheduler.py`.
- `run_scheduler` ставит задачи в очередь по интервалам; `run_tasks_worker` их выполняет.
- При ошибке задачи, помеченной в `FAST_RETRY_TASKS` (например, синхронизация с внешним
  сервисом), повтор — через `FAST_RETRY_INTERVAL` (5 минут), а не через полный интервал.
- Зависшие задачи (RUNNING > 30 минут) автоматически помечаются FAILED.

## Страницы (UI)

| URL | Представление | Доступ |
| --- | --- | --- |
| `/` | `app_page.MainPageView` | публично |
| `/search/` | `app_page.SearchView` | публично |
| `/uip/` | `app_page.UIPListView` | вход + роль «Админ»/«Просмотр» |
| `/cz/sync/` | `SyncTasksView` | админ |
| `/cz/nk/` | `NationalCatalogView` | админ |
| `/admin/` | Django admin | staff |
| `/api/docs/` | Swagger UI | публично |
| `/auth/` | стандартная аутентификация | — |

## Схема потоков данных

1. Внешние системы/план производства инициируют задания и запросы на УИП.
2. Система резервирует УИП в «Честном Знаке» и хранит их локально.
3. Локальные серверы «Молвест.Маркировка» пушат задания и отдают коды.
4. Коды сохраняются с иерархией; статусы синхронизируются с ЧЗ.
5. Смежные системы (1С и др.) получают статусы и данные через REST API.

## Конвенции

- Комментарии, `verbose_name`/`help_text`, UI — на русском.
- Явный `Meta.ordering`, `PROTECT` на FK, `TextChoices` для перечислений.
- Миграции gitignored (кроме `config/migrations/0001_initial.py`) — генерируются на каждой БД.
