# 06. Запуск, конфигурация и эксплуатация

## Требования

- Python 3.14+
- PostgreSQL
- Зависимости из `requirements.txt` (файл в UTF-16LE) и установка проекта `pip install -e .`
  (генерирует `config/_version.py` из git-тегов).

> Зависимости лежат в `.venv` (gitignored). Системный `python` может не иметь Django —
> активируйте venv (`.venv\Scripts\Activate.ps1`) или вызывайте `.venv\Scripts\python.exe`.

## Быстрый старт

```bash
# Всё сразу: web + scheduler + worker
python run_all.py

# По отдельности
uvicorn config.asgi:application --host 0.0.0.0 --port 8888
python manage.py run_scheduler
python manage.py run_tasks_worker

# Миграции
python manage.py makemigrations <app_label>
python manage.py migrate
python manage.py migrate --database archive
```

`run_all.py` привязывает uvicorn к LAN-IP машины и включает `--reload` только при `DEBUG=1`.

## Конфигурация (`config/.env`)

Единый источник настроек (читается `python-dotenv` в `config/settings.py`).

| Переменная | Описание |
| --- | --- |
| `DEBUG` | `1` — разработка, `0` — прод |
| `SECRET_KEY` | Секретный ключ |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` | Рабочая БД |
| `DB_ARCHIVE_*` | Архивная БД (по умолчанию = рабочая) |
| `TIME_ZONE`, `IS_TIME_ZONE` | Часовой пояс / `USE_TZ` |
| `UIP_RESERVE_LIMIT` | Лимит зарезервированных УИП (по умолчанию 10000) |
| `UIP_RESERVE_NOTIFICATION_EMAILS` | Получатели уведомлений |
| `UIP_SHORT_SHELF_LIFE_DAYS` | Срок годности (дней), ниже которого накапливается резерв УИП (по умолчанию 40) |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | Настройки почты |

- Адреса серверов маркировки — **per-factory** в модели `Factory` (`ip_address`, `port_address`),
  не в `.env`.
- Не читайте переменные окружения вне `settings.py`.

## Управляющие команды

| Команда | Назначение |
| --- | --- |
| `python manage.py run_scheduler` | Планировщик периодических задач |
| `python manage.py run_tasks_worker` | Воркер фоновых задач |
| `python manage.py reset_nk_sync` | Сброс зависшей блокировки синхронизации НК |
| `python manage.py archive_old_codes` | Архивация устаревших кодов |
| `python manage.py sync_molvest_reference` | Синхронизация справочников |

> `run_scheduler`/`run_tasks_worker` **не перезагружаются автоматически** — после изменения кода
> их нужно перезапускать (в отличие от `uvicorn --reload`).

## Периодические задачи

Расписание — список `SCHEDULE` в `app_scheduler/management/commands/run_scheduler.py`
(таблица в `README.md` может отставать). Основные:

- Обновление токена СУЗ — каждые 6 ч.
- Синхронизация заданий и кодов с «Молвест.Маркировка» — раз в час.
- Удаление/закрытие/архивация УИП, очистка логов — раз в сутки.
- Синхронизация справочников и Нац. каталога — раз в сутки.
- Мониторинг резерва и сгорания, регистрация УИП, архивация кодов — раз в сутки.
- Проверка активности продуктов на заводах — раз в сутки (перед накоплением резерва).
- Накопление резерва УИП на дни вперёд — раз в сутки.

## Мониторинг резерва УИП

| Заполнение | Действие |
| --- | --- |
| > 50% | Email-предупреждение |
| > 80% | Email-тревога |
| > 90% | Тревога + снятие устаревших УИП (не более 100 за запуск) |

Контроль сгорания: предупреждение, если до сгорания осталось < 7 дней.

### Проверка активности продуктов

Задача `sync_product_activity_task` (раз в сутки, до накопления резерва)
запрашивает у сервера каждого завода `GET {Factory.ip_address:port}/
workshop/api/v1/product-list/`. SKU, привязанные к линиям завода, но
отсутствующие в списке или помеченные `active=false`, деактивируются
(`ProductSKU.is_active=False`); если у продукта не остаётся активных SKU —
продукт тоже становится неактивным. При недоступности завода его SKU
не трогаются (сбой фиксируется в `failed_factories`).

### Накопление резерва УИП на дни вперёд

Задача `accumulate_short_shelf_life_reserve_task` (раз в сутки) доливает резерв
для активных SKU **обычного** формата (`ProductSKU.type_formation_uip =
«Обычный»`), у продукта которых срок годности меньше `UIP_SHORT_SHELF_LIFE_DAYS`
(по умолчанию 40 дней):

- окно дат — `[сегодня; сегодня + ProductSKU.reserve_days]` (поле «Резерв УИП,
  дней», по умолчанию 5 → 6 дат); доливается всё окно, пробелов не остаётся;
- на одну дату — ровно один УИП; если УИП уже есть, новый не создаётся;
  «сгоревший» (`deleted`) УИП повторно резервируется тем же номером;
- номер формируется локально (`build_local_party_number`);
- **по умолчанию `skip_cz=True` — создаются только ЧЕРНОВИКИ** без обращения
  к ЧЗ (оценка объёмов). При `skip_cz=False` номера резервируются в ЧЗ как
  «свои»: пачками ≤ 50 с паузой 10 сек между запросами; генерация номеров
  самим ЧЗ в автоматическом режиме не используется;
- при `skip_cz=False`, если заполнение резерва > 90% — накопление пропускается
  (чтобы не ухудшать). В режиме черновиков порог не применяется.

Жизненный цикл УИП (сгорание 30 дней, статусы, пороги 50/80/90%) не меняется.
Управляющая задача по умолчанию запускает безопасный режим (`skip_cz=True`);
для реального резервирования в ЧЗ — вызвать сервис с `skip_cz=False`.

## Миграции

- Миграции **gitignored** (`**/migrations/*.py`, кроме `config/migrations/0001_initial.py`) —
  после изменения моделей запускайте `makemigrations` + `migrate` на каждой БД/машине.
- CI генерирует миграции из моделей; деплой применяет их на обеих БД.

## Docker

`docker-compose.yml`: сервисы `web` + `db` (Postgres 15). Контейнер `web` выполняет
`makemigrations` + `migrate` (обе БД) → `collectstatic` → `run_all.py`.
Docker — не основной dev-поток.

## CI/CD

| Workflow | Файл | Что делает |
| --- | --- | --- |
| CI | `.github/workflows/ci.yml` | При push/PR: Postgres 15, установка зависимостей (iconv UTF-16 → UTF-8), `check`, `makemigrations`, `migrate` (обе БД), `collectstatic` |
| Deploy | `.github/workflows/deploy.yml` | При push в `main` или вручную: self-hosted runner (`linux, party-management`), вызов `deploy.sh` |

Ручной деплой: `./deploy.sh [branch] [--check-only]` (Linux) или
`.\deploy.ps1 -Branch main [-CheckOnly]` (Windows). Скрипт: `git pull --ff-only` → сборка →
ожидание БД → gate (`check`/`makemigrations`/`collectstatic`) → миграции обеих БД → `up -d --build`
→ пост-проверка.

## Версионирование

- setuptools-scm, версия из git-тегов в `config/_version.py`.
- Префиксы коммитов: `Feat:` / `Fix:` / `Docs:` (`fix` → patch, `feat` → minor).
- Релиз: `git tag -a v2.0.0 -m "..."`.

## Диагностика

- Swagger: `/api/docs/`, схема: `/api/schema/`.
- Панель статусов сервисов (СУЗ, заводы, 1С) в шапке — кэш 30 секунд.
- Виджет «Фоновые задачи» (админ) — расписание и ошибки (`/scheduler/status/`).
- Лента событий — `app_event.EventLog` через `log_event()`.
