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

# Tests (the only real suite; also runnable per class/method)
python manage.py test app_uip
python manage.py test app_uip.tests.ReserveUipsServiceTests.test_generate_count_loops

# Install in editable mode (generates config/_version.py from git tags)
pip install -e .
```

**`run_all.py`** binds uvicorn to the machine's LAN IP (`--host <local_ip>`, not `0.0.0.0`) and only adds `--reload` when `DEBUG=1`. The periodic-task schedule (which task runs at what interval) lives in the `SCHEDULE` list in `app_scheduler/management/commands/run_scheduler.py`, not in `tasks.py` (the README task table can lag behind it — trust `SCHEDULE`).

**Note:** dependencies live in `.venv` (gitignored). The system `python` on PATH has no Django — activate the venv (`.venv\Scripts\Activate.ps1` on Windows) or use `.venv\Scripts\python.exe` directly, otherwise `manage.py` fails with `ModuleNotFoundError: No module named 'django'`.

## Critical Conventions

- **Models extend `UUIDModel`** (from `app_helper.models`) which uses `UUID7Field` — time-ordered UUIDv7 primary keys. Don't use plain `models.Model`/`UUIDField` for core entities. **Exception:** log/feed tables like `app_event.EventLog` deliberately use `models.Model` with `CharField`/`JSONField` — follow the existing model's pattern.
- **`config/.env`** is the single config source (loaded by `python-dotenv` in `config/settings.py`). Never read env vars outside settings.
- **Migrations are gitignored (`**/migrations/*.py`)**, so new migration files don't show in `git status` and aren't shared via git — after a model change run `makemigrations` + `migrate` locally on each machine/DB (only `migrations/__init__.py` is committed). **Exception:** `config/migrations/0001_initial.py` is deliberately tracked in git.
- **No linting, formatting, or typechecking is enforced.** No ruff/flake8/mypy config exists. Follow existing code style: Russian-language `verbose_name`/`help_text`, explicit `Meta.ordering`, `PROTECT` on FKs, `TextChoices` for enums.
- **Test suites live in `app_uip/tests.py` (largest), `app_cz/tests.py` (external-sync reliability/watermark) and `app_scheduler/tests.py` (`_effective_interval`)** (Django `TestCase`/`SimpleTestCase`; `python manage.py test app_cz app_scheduler` or `app_uip`). Other apps' `tests.py` remain empty stubs. Run the relevant suite after touching a service/view/serializer. Tests need a reachable PostgreSQL (Django creates/drops a temp test DB) and mock CZ/HTTP calls with `unittest.mock.patch` — no network or SUZ account required.
- **Bugs and new functionality must ship with tests.** When you find/fix a bug, add a test that reproduces the error and proves the fix (guards against regression). When adding new functionality, add tests in the same change covering its happy paths, error paths, and edge cases. When behavior of existing functionality changes, update the corresponding tests accordingly.
- **Two databases.** Settings define `default` and `archive`. `app_cz.CISCodeArchive` is routed to the `archive` DB by `app_cz/routers.py` (denormalized snapshot, no FKs). `DB_ARCHIVE_*` env vars default to the same DB as `default`. Don't assume single-DB queries for archive data.
- **Unified UIP/code search** is `app_uip/views.py::api_search` at `GET /uip/api/v1/search/`. It mirrors the `/search/` page via `app_helper.search_helper.detect_search_type` + `clean_datamatrix_code` (strips GS/FNC1 before matching `CISCode.code`), accepts `q`/`type`/`page`/`page_size`, and returns `{query, search_type, count, page, page_size, results}`. `UIPActiveListSerializer.product_name` must read `product_sku.product.name` — `ProductSKU` has no `name` field (the old `product_sku.name` source raised on `/status_parties/active/`). `search` and `status_parties` are public (`AllowAny` is the DRF default and no login middleware exists); only `reserve-uips` requires an admin (now `IsAppAdmin`, not `IsAdminUser`).
- **Search performance: always try exact match first.** `filter_codes_by_query`/`filter_uips_by_query` (`app_helper/search_helper.py`) first run `code__exact`/`number__exact` (unique index → ms), and only fall back to `code__istartswith`/`number__iexact`. On the multi-million-row `CISCode` table a plain `Q(code__iexact) | Q(code__istartswith)` is a full scan (`UPPER(code) LIKE …`), which is why both `SearchView` (`app_page/views.py`) and `api_search` use the helpers — don't revert. The prefix fallback is backed by the functional index `cis_code_upper_prefix_idx` (`OpClass(Upper('code'), varchar_pattern_ops)`, model `Meta.indexes`; needs `django.contrib.postgres` in `INSTALLED_APPS`).
- **Roles/access use Django Groups, not `is_superuser`.** Helpers live in `app_helper/access.py`: `is_admin` (superuser OR `is_staff` OR group «Админ»), `can_view_uip` (admin OR group «Просмотр» OR `app_uip.view_uip`), `can_generate_uip` (admin OR `app_uip.add_uip`/`change_uip`). Groups «Админ» (all perms) and «Просмотр» (`view_uip`) are auto-created by a `post_migrate` receiver in `app_helper/signals.py` — no manual setup, and migrations/CI/deploy already run `migrate`. Use the `AdminRequiredMixin`/`admin_required`/`admin_required_json`/`generate_uip_required_json` helpers and the `is_admin`/`can_view_uip`/`can_generate_uip` template flags (from `config.context_processors.user_context`) instead of `user.is_superuser`. `/uip/` requires login + `can_view_uip`; generation is gated by the `uip` write permission; the `/search/` results page renders **cards** (not a table) and includes the external task number (`ProductionParty.external_number_task`) plus factory/workshop/line/article.

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
| `config` | Settings package AND a Django app: `ExternalService` model (signing/WMS/print services). Registered in `INSTALLED_APPS` as `config.apps.ConfigConfig` |

**Service layer pattern:** `app_cz`, `app_factory`, and `app_uip` use a `services/` subpackage (`app_uip/services/party_sync.py`, `app_uip/services/uip_reserve.py` — the multi-UIP reservation service covered by tests, `app_factory/services/molvest_reference_sync.py`, `app_factory/services/nk_sync_service.py`). `app_factory` additionally uses `selectors.py`. Other apps keep logic in views/serializers.

**NK → Product sync (`app_factory/services/nk_sync_service.py`) quirks (all verified against real NK data):**
- Supplier codes (SKU `article`/`other_codes`) come from attribute `Код товара в учетной системе поставщика` inside `good_attrs` (NOT `attrs` — that key doesn't exist in `/nk/product` responses). Values arrive comma-separated (`60379, 65942, ...`) and are split into a list.
- `ProductSKU` has NO `created_at` field — use `.order_by('id')` for "first created", not `order_by('created_at')`.
- A product can have MULTIPLE packagings of the same level (e.g. two boxes of different capacity). `ProductPackaging` has NO unique constraint on `(product, level)` — uniqueness is by `gtin` only. `sync_nk_to_products` keys `update_or_create` on `(product, gtin)`.
- `_parse_nc_packagings` maps `identified_by` level → packaging level: `trade-unit`→UNIT, `inner-pack`/`box`→GROUP, `layer`/`pallet`→TRANSPORT, and uses `multiplier` as `quantity_inside`.
- UIP sync looks up SKU by consumer (UNIT-level) GTIN via `find_sku_by_gtin`; if the product has no SKU it's skipped with «не найден SKU».
- **External task code sync** (`app_cz/services/code_sync.py`): the external Molvest server returns `sntins_camera` (applied) + `sntins_printer` (pending) and **dynamically moves codes printer→camera as they get applied**. After each `sync_codes_task`, `produced_quantity` (the «Факт» shown on the sync page) is recalculated as the count of APPLIED codes via `_update_party_produced_quantity`, not just from the `amount` field at task receipt — otherwise «План/Факт» goes stale.
- **Per-factory task-pull watermark (reliability).** `sync_external_parties_and_codes` pulls tasks changed since `Factory.external_sync_changed_since`, which is **per factory** and advances only on that factory's successful fetch (`WATERMARK_SAFETY_MARGIN = 5 min` is subtracted to survive in-flight edits / clock skew). `_fetch_external_tasks_changed_since` returns `None` (not `[]`) on network/response errors, so a failed factory keeps its watermark and retries the window next run; other factories are unaffected. Failures surface in `failed_factories`. The scheduled task raises on `is_error` so `DBTaskResult` becomes FAILED, and `run_scheduler` re-runs `sync_external_parties_codes` after `FAST_RETRY_INTERVAL` (5 min) instead of waiting the full hour. Manual full pull (tasks + codes) is `POST /cz/sync/all/` (`SyncAllTasksView`), not codes-only anymore.
- **External tasks without uuid are skipped.** `receive_external_task` (via `_extract_external_uuid`) accepts a task only if it carries `uuid_str`/`uuid_task` (the «Задание (Внешнее)» field must always be a uuid). The Molvest DB has thousands of old/test tasks with only numeric `id`/`task_id` and `uip=000...0` — those return `{'has_error': True, 'skipped_no_uuid': True}` and are NOT counted as sync errors in `sync_external_parties_and_codes` (tracked as `skipped_no_uuid`).
- **Two code-response formats (transition period).** `/codes/api/get_codes_by_task/` may return the new structured `codes: [{'code','level','parent_code'}]` (aggregation, present only on some lines) alongside the flat `sntins_camera`/`sntins_printer` lists. `sync_codes_task` prefers `codes` when non-empty and derives each code's APPLIED/PENDING status by matching it against the flat lists — keep that merge, since the structured format carries no status.

**CZ admin API views live in `app_cz/views.py`**, not `app_page`: NK sync/progress/product views, UIP sync/generate, and Molvest task sync views were moved there (URL names kept, paths are now under `/cz/`). Only `MainPageView`/`SearchView`/`UIPListView` remain in `app_page`. True API rate limits are enforced by `app_cz/services/rate_limit.py` (`wait_nk_endpoint` in `NationalCatalogClient._make_request`): `/nk/product` max 10 req/5 min, other NK methods 10 req/s. The limiter paces requests evenly (min interval = window/limit) — don't revert to burst-then-stall, it froze sync progress.

**NK sync is cross-process serialized.** `app_cz/services/nk_sync_state.py` holds a DB-backed singleton lock + progress (`NKSyncState`, row id=1, `SELECT ... FOR UPDATE`). Manual sync (web) and the scheduled `sync_national_catalog_task` (worker, currently 1×/day per `SCHEDULE`) both go through `try_start_sync()` — a concurrent attempt returns 409/skip instead of running in parallel (parallel syncs blew the True API limit → 400 batch errors and DB conflicts). Progress is written via `SyncProgress(dict)` proxy, so the NK page and the "Фоновые задачи" widget (`/scheduler/status/`, includes `nk_sync` state) both show it regardless of which process runs the sync. The lock has a **heartbeat stale timeout** (`LOCK_STALE_TIMEOUT = 10 min`): a lock whose `updated_at` hasn't refreshed in 10 min is considered dead (crashed container/process) and is auto-taken-over; use `python manage.py reset_nk_sync` to force-clear a stuck lock. **Graceful fallback:** `NKSyncState`'s migration is gitignored, so if the table is missing (e.g., Docker image built before `migrate`) the service auto-switches to per-process in-memory state instead of 500ing — endpoints keep working, but cross-process serialization only works once the migration is applied on that DB. **Note:** `run_scheduler`/`run_tasks_worker` don't auto-reload — after changing code, restart them (unlike `uvicorn --reload`).

**`/nk/product` batch failures are isolated.** True API returns 400 for the WHOLE 25-ID batch if even one `good_id` is problematic (deleted/foreign/kit). `NationalCatalogClient.fetch_products_resilient()` splits a failing batch recursively (`MAX_SPLIT_DEPTH=5`) so only genuinely-bad IDs are dropped and the rest are still saved; the real API error body is logged via `_log_response_error`. Don't revert to `client.get_products()` in a bare try/except — it turns one bad ID into 25 lost products.

## CI/CD

- **CI** (`.github/workflows/ci.yml`) runs on every push/PR with a Postgres 15 service: installs deps (iconv-converts the UTF-16 `requirements.txt`, strips `pywin32`/`git+`), then `manage.py check`, `makemigrations --check --dry-run` (non-blocking), `makemigrations`, `migrate` on **both** DBs (`--database archive` too), and `collectstatic`. Since migrations are gitignored, CI generates them from models — keep models and the tracked `config/migrations/0001_initial.py` consistent.
- **Deploy** (`.github/workflows/deploy.yml`) runs on push to `main` or manually on a **self-hosted runner** labeled `linux, party-management`, using repo variable `DEPLOY_PATH`; it just invokes `deploy.sh <branch>`.
- **Manual deploy scripts** `deploy.sh` (bash) / `deploy.ps1` (PowerShell): `git pull --ff-only` → `docker compose build` → wait for DB healthy → **gate** `check`/`makemigrations`/`collectstatic` (aborts on failure) → `migrate` both DBs → `docker compose up -d --build` → post-check. `--check-only`/`-CheckOnly` stops after the gate (no migrations/start). README «CI/CD» has the details.

## Gotchas

- **`config/_version.py`** is auto-generated by setuptools-scm and gitignored. If it's missing after clone, run `pip install -e .`
- **Versioning is tag-driven** (setuptools-scm). Commit subjects use `Feat:`/`Fix:`/`Docs:` prefixes; README maps `fix`→patch, `feat`→minor. Tag for a release: `git tag -a v2.0.0 -m "..."`.
- **`helper_info/`** (untracked) holds the authoritative Chestny Znak API specs: `True_API_GIS_MT.txt` (True API + National Catalog methods, rate limits — source for `app_cz/services/rate_limit.py`) and `API_СУЗ_3.0.txt` (СУЗ-Облако). Both are UTF-8 but with Cyrillic text; the .md files are readable summaries. Consult these before changing CZ integration behavior instead of guessing from the official site.
- **Background tasks** use `django-tasks-db` with queues: `default`, `emails`, `high-priority`. Task functions are in `app_scheduler/tasks.py`.
- **External system URLs** (marking servers) are per-factory, stored in `Factory.ip_address` and `Factory.port_address`. Not in `.env`.
- **UIP number format:** 14 GTIN digits + 6 date digits + 1-12 serial chars (regex-validated in model).
- **Status transitions** are audited in `UIPStatusLog` with source tracking (`admin`/`sync`/`api`/`service`).
- **Re-reservation of burned UIPs.** If an external use request arrives for a UIP that already exists in status `deleted` (burned after 30 days without registration), `_generate_local_uip` (deterministic local number) and `_reserve_own` (explicit `party_numbers`) re-reserve the **same number** in CZ via `reserve_parties_honest_sign`, then `_restore_burned_uip`/`restore_burned_uips` (`app_cz/services/party_service.py`) refresh `reservation_date`, clear `is_desync` and return the status to `reserved_local`. `skip_cz=True` and non-deleted existing UIPs keep the old "уже существует" behavior.
- **Management commands:** `app_cz/management/commands/archive_old_codes.py`, `app_cz/management/commands/reset_nk_sync.py`, `app_factory/management/commands/sync_molvest_reference.py`, plus `run_scheduler`/`run_tasks_worker` in `app_scheduler`.
- **Live events** are written via `log_event()` from `app_event/utils.py` (module/level/message/actor/metadata); use it instead of ad-hoc logging for user-visible feed entries.
- **`requirements.txt` is UTF-16LE-encoded** (Windows BOM `FF FE`), so Read/Edit tools see it as binary. The Dockerfile converts it via `iconv`; keep the encoding intact when editing.
- **Docker** (`docker-compose.yml`: web + Postgres 15) is available but not the primary dev flow. The container CMD runs `python app_cz/management/commands/reset_nk_sync.py` (executes the file directly, not via `manage.py`), then `makemigrations` + `migrate` **on both DBs** (`--database archive` too) + `collectstatic` + `run_all.py`. The Dockerfile patches `settings.py` to take `DB_HOST` from env via `sed` and strips `pywin32`/`git+` lines from the UTF-16 requirements — don't mirror those hacks in local code.
