# AGENTS.md

## Project Overview

Django 6.0.7 (ASGI/uvicorn) app for managing production batches and marking codes, integrated with "Chestny Znak" (Russian marking system). Python 3.14, PostgreSQL, DRF + drf-spectacular.

## Quick Commands

```bash
# Run everything (web + scheduler + worker) in one terminal
python run_all.py

# Individual components
uvicorn config.asgi:application --host 0.0.0.0 --port 8888
python manage.py run_scheduler
python manage.py run_tasks_worker

# Migrations
python manage.py makemigrations <app_label>
python manage.py migrate

# Install in editable mode (generates config/_version.py from git tags)
pip install -e .
```

**Note:** dependencies live in `.venv` (gitignored). The system `python` on PATH has no Django — activate the venv (`.venv\Scripts\Activate.ps1` on Windows) or use `.venv\Scripts\python.exe` directly, otherwise `manage.py` fails with `ModuleNotFoundError: No module named 'django'`.

## Critical Conventions

- **Models extend `UUIDModel`** (from `app_helper.models`) which uses `UUID7Field` — time-ordered UUIDv7 primary keys. Don't use plain `models.Model`/`UUIDField` for core entities. **Exception:** log/feed tables like `app_event.EventLog` deliberately use `models.Model` with `CharField`/`JSONField` — follow the existing model's pattern.
- **`config/.env`** is the single config source (loaded by `python-dotenv` in `config/settings.py`). Never read env vars outside settings.
- **Migrations are gitignored (`**/migrations/*.py`)**, so new migration files don't show in `git status` and aren't shared via git — after a model change run `makemigrations` + `migrate` locally on each machine/DB (only `migrations/__init__.py` is committed). **Exception:** `config/migrations/0001_initial.py` is deliberately tracked in git.
- **No linting, formatting, or typechecking is enforced.** No ruff/flake8/mypy config exists. Follow existing code style: Russian-language `verbose_name`/`help_text`, explicit `Meta.ordering`, `PROTECT` on FKs, `TextChoices` for enums.
- **No tests exist.** All `tests.py` files are empty stubs.
- **Two databases.** Settings define `default` and `archive`. `app_cz.CISCodeArchive` is routed to the `archive` DB by `app_cz/routers.py` (denormalized snapshot, no FKs). `DB_ARCHIVE_*` env vars default to the same DB as `default`. Don't assume single-DB queries for archive data.

## Architecture

| App | Purpose |
|---|---|
| `app_factory` | Reference data: factories, workshops, lines, products, GTIN/SKU |
| `app_uip` | Core entities: `UIP`, `ProductionParty`, `UIPStatusLog`. All status changes go through `UIP.change_status()` |
| `app_cz` | Chestny Znak integration: CIS codes, SUZ accounts, archive (`CISCodeArchive`), services (`suz_client`, `true_api_client`, `party_service`, `code_client`, `code_sync`, `code_archive`, `reserve_monitor`, `national_catalog_client`, `rate_limit`) |
| `app_scheduler` | Periodic tasks + management commands (`run_scheduler`, `run_tasks_worker`) |
| `app_page` | Django template-based UI pages (main, search, UIP list) |
| `app_helper` | Shared utilities: `UUIDModel`, middleware, search/sign/user helpers, `load_tracker` |
| `app_wms` | Warehouse module (skeleton) |
| `app_event` | Live event feed: `EventLog` model (`models.Model`, not UUIDModel) + `log_event()` helper in `utils.py` |
| `config` | Settings package AND a Django app: `ExternalService` model (signing/WMS/print services), default row data-migrated in `0002_seed_default_services` |

**Service layer pattern:** `app_cz`, `app_factory`, and `app_uip` use a `services/` subpackage (`app_uip/services/party_sync.py`, `app_factory/services/molvest_reference_sync.py`, `app_factory/services/nk_sync_service.py`). `app_factory` additionally uses `selectors.py`. Other apps keep logic in views/serializers.

**CZ admin API views live in `app_cz/views.py`**, not `app_page`: NK sync/progress/product views, UIP sync/generate, and Molvest task sync views were moved there (URL names kept, paths are now under `/cz/`). Only `MainPageView`/`SearchView`/`UIPListView` remain in `app_page`. True API rate limits are enforced by `app_cz/services/rate_limit.py` (`wait_nk_endpoint` in `NationalCatalogClient._make_request`): `/nk/product` max 10 req/5 min, other NK methods 10 req/s. The limiter paces requests evenly (min interval = window/limit) — don't revert to burst-then-stall, it froze sync progress.

**NK sync is cross-process serialized.** `app_cz/services/nk_sync_state.py` holds a DB-backed singleton lock + progress (`NKSyncState`, row id=1, `SELECT ... FOR UPDATE`). Manual sync (web) and hourly `sync_national_catalog_task` (worker) both go through `try_start_sync()` — a concurrent attempt returns 409/skip instead of running in parallel (parallel syncs blew the True API limit → 400 batch errors and DB conflicts). Progress is written via `SyncProgress(dict)` proxy, so the NK page and the "Фоновые задачи" widget (`/scheduler/status/`, includes `nk_sync` state) both show it regardless of which process runs the sync. The lock has a **heartbeat stale timeout** (`LOCK_STALE_TIMEOUT = 10 min`): a lock whose `updated_at` hasn't refreshed in 10 min is considered dead (crashed container/process) and is auto-taken-over; use `python manage.py reset_nk_sync` to force-clear a stuck lock. **Graceful fallback:** `NKSyncState`'s migration is gitignored, so if the table is missing (e.g., Docker image built before `migrate`) the service auto-switches to per-process in-memory state instead of 500ing — endpoints keep working, but cross-process serialization only works once the migration is applied on that DB. **Note:** `run_scheduler`/`run_tasks_worker` don't auto-reload — after changing code, restart them (unlike `uvicorn --reload`).

**`/nk/product` batch failures are isolated.** True API returns 400 for the WHOLE 25-ID batch if even one `good_id` is problematic (deleted/foreign/kit). `NationalCatalogClient.fetch_products_resilient()` splits a failing batch recursively (`MAX_SPLIT_DEPTH=5`) so only genuinely-bad IDs are dropped and the rest are still saved; the real API error body is logged via `_log_response_error`. Don't revert to `client.get_products()` in a bare try/except — it turns one bad ID into 25 lost products.

## Gotchas

- **`config/_version.py`** is auto-generated by setuptools-scm and gitignored. If it's missing after clone, run `pip install -e .`
- **`helper_info/`** (untracked) holds the authoritative Chestny Znak API specs: `True_API_GIS_MT.txt` (True API + National Catalog methods, rate limits — source for `app_cz/services/rate_limit.py`) and `API_СУЗ_3.0.txt` (СУЗ-Облако). Both are UTF-8 but with Cyrillic text; the .md files are readable summaries. Consult these before changing CZ integration behavior instead of guessing from the official site.
- **Background tasks** use `django-tasks-db` with queues: `default`, `emails`, `high-priority`. Task functions are in `app_scheduler/tasks.py`.
- **External system URLs** (marking servers) are per-factory, stored in `Factory.ip_address` and `Factory.port_address`. Not in `.env`.
- **UIP number format:** 14 GTIN digits + 6 date digits + 1-12 serial chars (regex-validated in model).
- **Status transitions** are audited in `UIPStatusLog` with source tracking (`admin`/`sync`/`api`/`service`).
- **Management commands:** `app_cz/management/commands/archive_old_codes.py`, `app_factory/management/commands/sync_molvest_reference.py`, plus `run_scheduler`/`run_tasks_worker` in `app_scheduler`.
- **Live events** are written via `log_event()` from `app_event/utils.py` (module/level/message/actor/metadata); use it instead of ad-hoc logging for user-visible feed entries.
- **`requirements.txt` is UTF-16LE-encoded** (Windows BOM `FF FE`), so Read/Edit tools see it as binary. The Dockerfile converts it via `iconv`; keep the encoding intact when editing.
- **Docker** (`docker-compose.yml`: web + Postgres 15) is available but not the primary dev flow. Container runs `migrate` + `run_all.py`, and the Dockerfile patches `settings.py` to take `DB_HOST` from env via `sed` — don't mirror that hack in local code.
