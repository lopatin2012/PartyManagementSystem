# AGENTS.md

## Project Overview

Django 6.0.7 (ASGI/uvicorn, Python 3.14) app for managing production batches and marking codes, integrated with "Chestny Znak" (Russian marking system). PostgreSQL, DRF + drf-spectacular, `django-tasks-db` background tasks. UI text and model `verbose_name`s are Russian.

`README.md` is the full product description; this file is the agent-facing gotcha sheet. `docs/` is derived prose — the code and this file are the source of truth.

## Quick Commands

```bash
# Everything (web + scheduler + worker) in one terminal
python run_all.py

# Individual components
uvicorn config.asgi:application --host 0.0.0.0 --port 8888
python manage.py run_scheduler
python manage.py run_tasks_worker
python manage.py run_scheduler --once   # enqueue every scheduled task once, then exit (testing)

# Migrations
python manage.py makemigrations <app_label>
python manage.py migrate                # also: migrate --database archive

# Tests (all real suites; a single app/class/method also works)
python manage.py test app_uip app_cz app_factory app_event app_scheduler app_page app_helper
python manage.py test app_uip.tests.ReserveUipsServiceTests.test_generate_count_loops

# Fresh checkout (inside .venv). pyproject has NO [project].dependencies, so
# `pip install -e .` alone does NOT install Django/DRF — requirements first.
pip install -r requirements.txt
pip install -e .   # generates config/_version.py from git tags (setuptools-scm)
```

- **Use the venv.** Dependencies live in `.venv` (gitignored); the system `python` has no Django. Activate it (`.venv\Scripts\Activate.ps1` on Windows) or call `.venv\Scripts\python.exe`, otherwise `manage.py` fails with `ModuleNotFoundError: No module named 'django'`.
- **`run_all.py`** binds uvicorn to the machine's LAN IP (not `0.0.0.0`) and adds `--reload` unless `DEBUG` is explicitly `0`. Restart `run_scheduler`/`run_tasks_worker` manually after code changes — they don't reload (unlike `uvicorn --reload`).

## Scheduled tasks

- **The `SCHEDULE` list in `app_scheduler/management/commands/run_scheduler.py` is the source of truth** for what runs at what interval (the README task table can lag).
- `FIXED_DAILY_TIMES` (same module) makes a task ignore its interval and run once a day at a given **local** time (`is_fixed_daily_due`/`next_fixed_daily_run`; currently only `accumulate_short_shelf_life_reserve` at 00:30). `run_scheduler` and `/scheduler/status/` both honor it.
- The name→function mapping is centralized in `app_scheduler/registry.py::get_scheduled_tasks()`, shared by `run_scheduler`, `/scheduler/status/` and `/scheduler/run/<name>/`. A new scheduled task needs entries in **both** `SCHEDULE` and the registry (`SchedulerRegistryTests` asserts they match).
- A FAILED `sync_external_parties_codes` is retried after `FAST_RETRY_INTERVAL` (5 min) instead of the full hour.
- **Task progress:** `app_scheduler/progress.py` (`set_task_progress`/`get_task_progress`/`clear_task_progress`) stores live progress in the `app_scheduler.TaskProgress` model (one row per task). Tasks opt in via a `progress_name`; `/scheduler/status/` adds `is_running`/`is_queued`/`started_at`/`progress`, and the admin «Фоновые задачи» modal shows a bar and polls every 5 s. Writes are best-effort — a missing table never fails a task.
- **Manual task run:** the modal's «Запуск» button POSTs `/scheduler/run/<name>/` (`SchedulerRunView`, guarded by `admin_required_json`); it enqueues the task (the worker executes it) and returns 409 if a READY/RUNNING run for the same task exists. `<name>` is the `SCHEDULE` key.

## Architecture

| App | Purpose |
|---|---|
| `app_factory` | Reference data: factories, workshops, lines, products, GTIN/SKU. Also `UserFactory` (user→factory) and `selectors.py` |
| `app_uip` | Core entities: `UIP`, `ProductionParty`, `UIPStatusLog`. All status changes go through `UIP.change_status()` |
| `app_cz` | Chestny Znak integration: CIS codes, SUZ accounts, archive (`CISCodeArchive`), services (`suz_client`, `true_api_client`, `party_service`, `code_client`, `code_sync`, `code_status`, `code_archive`, `desync_resolution`, `nk_sync_state`, `reserve_monitor`, `national_catalog_client`, `rate_limit`) |
| `app_scheduler` | Periodic tasks + management commands (`run_scheduler`, `run_tasks_worker`, `registry.py`, `progress.py`, `tasks.py`) |
| `app_page` | Django-template UI: main, search, UIP list, product control (`/products/`), SUZ orders (`/suz/`) |
| `app_helper` | Shared utilities: `UUIDModel`, middleware, `access.py`, search/sign/user helpers, `load_tracker` |
| `app_event` | Live event feed + observability: `EventLog` (`models.Model`, not UUIDModel) + `log_event()` (`utils.py`); `HealthCheck`, `NotificationRecipient`, `services/health.py` |
| `app_wms` | Warehouse module (empty skeleton) |
| `config` | Settings package **and** a Django app: `ExternalService` model, registered as `config.apps.ConfigConfig` |

- **CZ admin API views live in `app_cz/views.py`**, not `app_page`: NK sync/progress/product views, UIP sync/generate, and Molvest task sync views were moved there (URL names kept, paths are now under `/cz/`). Only `MainPageView`/`SearchView`/`UIPListView` and the product-control views (`ProductControlView`/`ProductControlDetailView`) remain in `app_page`.
- **URL ownership:** the `/uip/` and `/products/` HTML pages are routed by `app_page/urls.py`; `app_uip/urls.py` holds only the `/uip/api/v1/...` DRF routes (`search`, `reserve-uips`, `status_parties`).
- **SUZ orders page (`/suz/`, nav «СУЗ» after «Продукция», admin-only).** `SuzOrdersView` (`app_page/views.py`) renders `templates/suz/main.html`; data comes from `app_cz/services/order_service.py::get_suz_orders` → `POST /api/v3/orders/search?omsId=...` with `clientToken` = the active `SUZAccount.dynamic_token`. Columns: `orderId`, дата создания (`createdTimestamp`, ms), количество товаров (buffers/GTIN), `serviceProviderId`, `totalCodes`/`availableCodes`, статус (`orderStatus` → `ORDER_STATUS_LABELS`). Filters `order_id`/`status`, page size 50 (max 100); no active account/token, HTTP and network errors render as a message, not a 500.
- **Service layer pattern:** `app_cz`, `app_factory`, `app_uip` and `app_event` use `services/` subpackages (e.g. `app_uip/services/uip_reserve.py` — the multi-UIP reservation service covered by tests, `app_uip/services/reserve_accumulation.py`). Other apps keep logic in views/serializers. `app_helper/external.py::safe_external_call(func, ...)` wraps non-critical external calls, logs + writes an `EventLog`, and returns `(result, error)` instead of raising.

## Critical conventions

- **Models extend `UUIDModel`** (`app_helper.models`) — time-ordered UUIDv7 PKs via `UUID7Field`. Don't use plain `models.Model`/`UUIDField` for core entities. **Exception:** log/feed tables like `app_event.EventLog` deliberately use `models.Model` with `CharField`/`JSONField`.
- **`config/.env` is the only config source** (loaded by `python-dotenv` in `config/settings.py`). It is gitignored and there is no `.env.example`; a fresh checkout must create one. `help.txt` documents core DB/DEBUG/version vars; README documents the `UIP_*`/`HEALTH_*`/`EMAIL_*` ones. `SESSION_COOKIE_NAME` defaults to the **stable** `pms_sessionid` (a per-process random default invalidated the session on every restart, forcing re-login).
- **Migrations are gitignored (`**/migrations/*.py`)** — they don't show in `git status` and aren't shared via git. After a model change run `makemigrations` + `migrate` on each machine/DB. **Exception:** `config/migrations/0001_initial.py` is deliberately tracked.
- **No lint, format or typecheck is enforced** (no ruff/flake8/mypy config). Match existing style: Russian `verbose_name`/`help_text`, explicit `Meta.ordering`, `PROTECT` on FKs, `TextChoices` for enums.
- **Two databases.** Settings define `default` and `archive`. `app_cz.CISCodeArchive` is routed to `archive` by `app_cz/routers.py` (denormalized snapshot, no FKs). `DB_ARCHIVE_*` env vars default to the `default` values. Use `.using('archive')` for archive queries.
- **Access uses Django Groups, not `is_superuser`.** `app_helper/access.py`: `is_admin` (superuser OR `is_staff` OR group «Админ»), `can_view_uip` (admin OR group «Просмотр» OR `app_uip.view_uip`), `can_generate_uip` (admin OR `app_uip.add_uip`/`change_uip`). Groups «Админ» (all perms), «Просмотр» (`view_uip`) and «Мониторинг» are auto-created by a `post_migrate` receiver in `app_helper/signals.py`. Use `AdminRequiredMixin`/`admin_required`/`admin_required_json`/`generate_uip_required_json` and the `is_admin`/`can_view_uip`/`can_generate_uip` template flags (`config.context_processors.user_context`) instead of `user.is_superuser`. `/uip/` requires login + `can_view_uip`; generation is gated by the `uip` write permission.
- **Per-factory access (products & UIPs).** `get_user_factory(user)` (backed by `app_factory.UserFactory`, user→factory, edited in `/admin/`) scopes a **non-admin** user to one factory. **Admins (superuser / `is_staff` / group «Админ») and unbound users get `None` → they see everything.** Helpers `filter_uips_for_user(qs)`, `user_can_access_uip`, `user_can_access_sku`: a UIP is «свой» if its SKU has a `ProductProductionLocation` at the factory **or** it has a `ProductionParty` at the factory. Applied at: `UIPListView` (`/uip/`, plus the scoped `get_available_products(factory=...)` generation dropdown), `GenerateUIPView` (`/cz/uip/generate/` → 403 for a foreign SKU), the product card, and the per-UIP DRF actions (`api_reserve_draft_uip`/`api_report_uip`/`api_check_uip_cz` → 403; currently inert because those stay `IsAppAdmin` and admins bypass).
- **`/products/` (product control).** `ProductControlView` lists products with search (`q` by name/article/GTIN) and filters (group/activity/factory); `ProductControlDetailView` (`/products/<uuid>/`) shows the product plus its `type_formation_uip`/`reserve_days` SKUs, Molvest `sku_links`, packagings and production locations, and allows inline POST edits (`target=product|sku|packaging|location`; `get_object_or_404` scoped to the product, and a bound user's foreign SKU/location ids also → 404). Any logged-in user may open it; nav «Продукция» shows for authenticated users. This is a UI view, not the DRF `/factory/api/...` dictionaries.

## Tests

- **Real suites:** `app_uip/tests.py` (largest), `app_cz/tests.py` (external-sync reliability/watermark, code sync, CZ status mapping, manual UIP, marking date, CISCode admin/paginator), `app_factory/tests.py` (product activity/`sku_links`, NK shelf life, shared packaging), `app_event/tests.py` (health checks/alerts), `app_scheduler/tests.py` (`effective_interval`, fixed daily time, status API, task progress/manual run), `app_page/tests.py` (product-control page, factory scoping, inline edits, status-panel render), `app_helper/tests.py` (load-stats message, `diagnose_service` check names, stable session cookie). `app_wms/tests.py` is an empty stub.
- Tests need a reachable PostgreSQL (Django creates/drops a temp test DB) and mock CZ/HTTP calls with `unittest.mock.patch` — no network or SUZ account required.
- Page-render tests need the `STATIC_OVERRIDE` trick (`ManifestStaticFilesStorage` has no manifest in tests) and should patch `config.context_processors.check_factories` / `app_helper.service_helper.diagnose_service` so the status widget doesn't hit the network.
- **Everything ships with tests.** A bug fix adds a test that reproduces it and proves the fix; new functionality covers happy paths, error paths and edge cases; changed behavior updates the corresponding tests. Run the relevant suite after touching a service/view/serializer.

## Search (UIP & marking codes)

- **Unified search** is `app_uip/views.py::api_search` at `GET /uip/api/v1/search/`. It mirrors the `/search/` page via `app_helper.search_helper.detect_search_type` + `clean_datamatrix_code`, accepts `q`/`type`/`page`/`page_size` (default 25, max 100), and returns `{query, search_type, count, page, page_size, in_archive, results}`.
- **Always try exact match first.** `filter_codes_by_query`/`filter_uips_by_query` (`app_helper/search_helper.py`) run `code__exact`/`number__exact` (unique index → ms) first, then fall back to `code__istartswith`/`number__iexact`. On the multi-million-row `CISCode` table a plain `Q(code__iexact) | Q(code__istartswith)` is a full scan (`UPPER(code) LIKE …`) — don't revert. The prefix fallback is backed by the functional index `cis_code_upper_prefix_idx` (`OpClass(Upper('code'), varchar_pattern_ops)` in `Meta.indexes`; needs `django.contrib.postgres` in `INSTALLED_APPS`).
- **Codes are stored WITH the GS separator `\x1d`** (before AI 93), e.g. `0104601751025838215umU':\x1d93E59s`, so `filter_codes_by_query` checks BOTH the raw query (as the scanner returns it) and the `clean_datamatrix_code`-stripped form. Do NOT clean the query before exact match or scanner searches break.
- **Archive fallback.** Codes older than 45 days move nightly from `CISCode` to `CISCodeArchive` (separate `archive` DB) by `app_cz/services/code_archive.py`. Both `api_search` and `SearchView` query the working table first, then — only for `code` searches — `CISCodeArchive.objects.using('archive')`. The API sets `in_archive: true` and serializes archive rows with `CISCodeArchiveSearchResultSerializer` (the page shows «Код найден в архиве»). UIPs are never searched in the archive.
- `UIPActiveListSerializer.product_name` must read `product_sku.product.name` — `ProductSKU` has no `name` field (the old `product_sku.name` source raised on `/status_parties/active/`). `search` and `status_parties` are public (`AllowAny` is the DRF default and no login middleware exists); only `reserve-uips` requires an admin (now `IsAppAdmin`, not `IsAdminUser`).
- The `/search/` results page renders **cards** (not a table) and includes the external task number (`ProductionParty.external_number_task`) plus factory/workshop/line/article.

## CZ / Molvest integration gotchas

**External task code sync (`app_cz/services/code_sync.py`).**
- The external Molvest server returns `sntins_camera` (applied) + `sntins_printer` (pending) and dynamically moves codes printer→camera as they get applied. After each `sync_codes_task`, `produced_quantity` (the «Факт» shown on the sync page) is recalculated as the count of APPLIED codes via `_update_party_produced_quantity` — not just from the `amount` field at task receipt, otherwise «План/Факт» goes stale.
- Deleted external tasks (`status=DELETED`) are marked `SYNCED`, not `PENDING`, in both `receive_external_task` and `sync_all_external_tasks`, so they don't hang in «Ожидают синхронизации».
- `sync_all_external_tasks` only pulls codes for parties in `CODE_SYNC_STATUSES` (`work`/`closed`) whose `external_created_at` (external `datetime_create`, mapped in `receive_external_task`) is within `CODE_SYNC_WINDOW_DAYS` (3) — «Создано»/«Завершено» and older tasks are skipped (volume control; stage-1 task intake still stores all statuses). After the loop, the ineligible `PENDING` parties are bulk-marked `SYNCED` so «Ожидают синхронизации» on `/cz/sync/` doesn't grow without bound — don't revert to leaving them `PENDING` (a task re-pushed by the external service returns to `PENDING` via `receive_external_task`).
- **Two code-response formats (transition period):** `/codes/api/get_codes_by_task/` may return the new structured `codes: [{'code','level','parent_code'}]` (aggregation, present only on some lines) alongside the flat `sntins_camera`/`sntins_printer` lists. `sync_codes_task` prefers `codes` when non-empty and derives each code's APPLIED/PENDING status by matching it against the flat lists — keep that merge, since the structured format carries no status.

**Per-factory task-pull watermark (reliability).** `sync_external_parties_and_codes` pulls tasks changed since `Factory.external_sync_changed_since`, which is **per factory** and advances only on that factory's successful fetch (`WATERMARK_SAFETY_MARGIN = 5 min` is subtracted to survive in-flight edits / clock skew). `_fetch_external_tasks_changed_since` returns `None` (not `[]`) on network/response errors, so a failed factory keeps its watermark and retries the window next run; other factories are unaffected. Failures surface in `failed_factories`. The scheduled task raises on `is_error` so `DBTaskResult` becomes FAILED. Manual full pull (tasks + codes) is `POST /cz/sync/all/` (`SyncAllTasksView`), not codes-only anymore.

**Tasks without uuid are skipped.** `receive_external_task` (via `_extract_external_uuid`) accepts a task only if it carries `uuid_str`/`uuid_task` (the «Задание (Внешнее)» field must always be a uuid). The Molvest DB has thousands of old/test tasks with only numeric `id`/`task_id` and `uip=000...0` — those return `{'has_error': True, 'skipped_no_uuid': True}` and are NOT counted as sync errors (tracked as `skipped_no_uuid`).

**Desync auto-resolution (`app_cz/services/desync_resolution.py`, scheduled `resolve_desync_uips` hourly).** For every `is_desync=True` UIP (except `draft`/`deleted`) it checks the UIP's codes from its external tasks — local `CISCode` rows, and if there are none, one code fetched from the factory server (`get_any_code_for_task`, like `register_uip`) — against CZ via batched `/cises/info` (per product group). An `APPLIED`/`INTRODUCED` code means the marking report was formed: `reserved_cz`/`reserved_local` → `registered` (then the usual `closed`/`archived` ladder), while `registered`/`closed`/`archived` just lose the `is_desync` flag. UIPs with no applied code stay in desync for manual review. Note `sync_parties` re-flags a registered UIP that CZ still lists as reserved, so for such UIPs the flag can be set/cleared each cycle until CZ drops them from the reserve list. `sync_parties` samples only ONE code per UIP (`_collect_uip_codes`); the resolver checks up to `MAX_CODES_PER_UIP`.

**Desync UI on `/uip/`.** The toolbar «В рассинхроне» link filters `is_desync=True` (`?desync=1`, handled in `UIPListView`); for a desync row an admin sees a «Проверить в ЧЗ» icon that POSTs `/cz/api/check-uip-cz/` (`api_check_uip_cz` → `reserve_party_numbers_cz`, which reserves directly in CZ bypassing the local duplicate check):
- success = number was free → back to `reserved_local` + `is_desync=False`;
- CZ refusal → the UIP's codes are checked in CZ (`/cises/info`): `APPLIED`/`INTRODUCED` → `registered` + `is_desync=False`;
- if the code doesn't confirm it, the CZ reserved-parties list (`party_service.get_reserved_party_numbers`) decides — number present → status set to `reserved_local`/`reserved_cz` (drafts included, `is_desync=False`), absent → `registered` (already registered) + `is_desync=False`;
- the list being unavailable leaves the UIP in desync.
All outcomes return 200 with `result` = `reserved`/`registered`/`desync`.

**True API rate limits** are enforced by `app_cz/services/rate_limit.py` (`wait_nk_endpoint` in `NationalCatalogClient._make_request`): `/nk/product` max 10 req/5 min, other NK methods 10 req/s. The limiter paces requests evenly (min interval = window/limit) — don't revert to burst-then-stall, it froze sync progress.

**NK → Product sync (`app_factory/services/nk_sync_service.py`, verified against real NK data).**
- Supplier codes (SKU `article`/`other_codes`) come from attribute `Код товара в учетной системе поставщика` inside `good_attrs` (NOT `attrs` — that key doesn't exist in `/nk/product` responses). Values arrive comma-separated (`60379, 65942, ...`) and are split into a list.
- `ProductSKU` has NO `created_at` field — use `.order_by('id')` for "first created".
- A product can have MULTIPLE packagings of the same level (e.g. two boxes of different capacity). `ProductPackaging` has NO unique constraint on `(product, level)` — uniqueness is by `gtin` only. `sync_nk_to_products` keys `update_or_create` on `(product, gtin)`.
- `_parse_nc_packagings` maps `identified_by` level → packaging level: `trade-unit`→UNIT, `inner-pack`/`box`→GROUP, `layer`/`pallet`→TRANSPORT, and uses `multiplier` as `quantity_inside`.
- UIP sync looks up SKU by consumer (UNIT-level) GTIN via `find_sku_by_gtin`; if the product has no SKU it's skipped with «не найден SKU».

**`/nk/product` batch failures are isolated.** True API returns 400 for the WHOLE 25-ID batch if even one `good_id` is problematic (deleted/foreign/kit). `NationalCatalogClient.fetch_products_resilient()` splits a failing batch recursively (`MAX_SPLIT_DEPTH=5`) so only genuinely-bad IDs are dropped and the rest are still saved; the real API error body is logged via `_log_response_error`. Don't revert to `client.get_products()` in a bare try/except — it turns one bad ID into 25 lost products.

**NK sync is cross-process serialized.** `app_cz/services/nk_sync_state.py` holds a DB-backed singleton lock + progress (`NKSyncState`, row id=1, `SELECT ... FOR UPDATE`). Manual sync (web) and the scheduled `sync_national_catalog_task` (worker, currently 1×/day per `SCHEDULE`) both go through `try_start_sync()` — a concurrent attempt returns 409/skip instead of running in parallel (parallel syncs blew the True API limit → 400 batch errors and DB conflicts). Progress is written via the `SyncProgress(dict)` proxy, so the NK page and the "Фоновые задачи" widget (`/scheduler/status/`) both show it regardless of which process runs the sync. The lock has a **heartbeat stale timeout** (`LOCK_STALE_TIMEOUT = 10 min`) with auto-takeover of a dead lock; use `python manage.py reset_nk_sync` to force-clear a stuck lock. **Graceful fallback:** the `NKSyncState` migration is gitignored, so if the table is missing (e.g. a Docker image built before `migrate`) the service auto-switches to per-process in-memory state instead of 500ing — endpoints keep working, but cross-process serialization only works once the migration is applied on that DB.

**СУЗ dynamic token refresh (`app_cz/services/suz_client.py`).** `refresh_suz_dynamic_token()` retries the whole `/auth/key` → sign → `simpleSignIn/{connection_identifier}` chain up to `SUZ_TOKEN_REFRESH_ATTEMPTS` (3) times and then gives up. `ensure_suz_token_valid()` refreshes **proactively** when the stored token expires within `SUZ_TOKEN_REFRESH_THRESHOLD` (1 h). The scheduled `refresh_suz_token` task (every 6 h, token lifetime stubbed at 8 h) delegates to `ensure_suz_token_valid()`, and `app_event.services.health._diagnose` also calls it every 5 min before `check_suz_token` — so a token is refreshed ~1 h before expiry regardless of the 6 h schedule (the old 6 h-interval/8 h-lifetime gap caused the «Динамический токен СУЗ истёк» alert).

## Reserve monitoring & accumulation

- `app_cz/services/reserve_monitor.py` fill thresholds: `>60%` warn, `>75%` critical, `>95%` release obsolete reserved UIPs (registration sends an application report; marking date = `min(production_date, today)`, expiry falls back to the marking date when the party has no `expiration_datetime`; UIPs without a production date are skipped).
- **`registered` is confirmed only by an actual reserve release.** `register_uip` / `close_party_reservation` send the marking report, then check the CZ reserved-parties list (`get_reserved_party_numbers`, `reserve_monitor._confirm_reserve_release`): a UIP becomes `registered` **only if its number left the reserve within `BURN_DAYS` (30) days** of reservation. Still reserved → the report didn't release the party → stay `reserved_*` (a `200` report with a code that belongs to another party no longer yields a false `registered`, which `sync_parties` used to flag as desync); left after 30 days → burned, not registered; list unavailable → not confirmed. This is the fix for «УИП стал Зарегистрирован, хотя не снялся с резерва».
- `app_uip/services/reserve_accumulation.py` (daily 00:30) keeps one UIP per date over `[today; today + ProductSKU.reserve_days]` for active `TypeFormationUIP.general` SKUs whose `Product.shelf_life_in_days <= UIP_SHORT_SHELF_LIFE_DAYS` (default 45, inclusive).
- The band `41..UIP_SHORT_SHELF_LIFE_DAYS` is reserved in CZ with the **locally-built** number (`reserved_local`); shelf life `<= UIP_DRAFT_SHELF_LIFE_DAYS` (default 40) creates **drafts** — except production dates on/after the code constant `SHORT_SHELF_LIFE_RESERVE_FROM = date(2027, 3, 1)`, which are reserved in CZ too (decision per UIP in `_should_reserve_entry`, automatic accumulation only). An explicit `skip_cz=False` also reserves the short band. `UIP_DRAFT_MODE` no longer affects accumulation (it still governs `generate_uip`, **except** manual local generation on `/uip/`); reservation is skipped when reserve > 95%.
- The same `reserve_accumulation.should_reserve_in_cz(sku)` predicate is reused by `GenerateUIPView` (`/cz/uip/generate/`, `mode='local'`): a product with `shelf_life_in_days > UIP_DRAFT_SHELF_LIFE_DAYS` is reserved in CZ even when `UIP_DRAFT_MODE=1`, while shorter shelf life keeps the configured draft behavior.
- `reserve_parties_honest_sign` tolerates numbers already reserved in CZ: CZ rejects the whole batch with «В списке партий содержатся дубликаты», so it drops those (returned in `already_reserved`), retries the rest, returns success; the accumulation then records the already-reserved numbers locally as `reserved_local`.
- UIP creation is idempotent (`UIP.objects.get_or_create(number=...)`) in both `sync_parties_from_cz` and reserve accumulation, because `sync_parties` (every 30 min) and the accumulation can target the same deterministic number concurrently (check-then-insert race → `IntegrityError` on `app_uip_uip_number_key`).

## Observability & alerts (`app_event`)

`run_health_checks()` (scheduled `check_system_health` every 5 min; toggles `HEALTH_CHECK_ENABLED`/`SYSTEM_ALERTS_ENABLED`) writes one `HealthCheck` row per check (db/suz/signatures/factories/load/summary). A **failure is confirmed only after `HEALTH_CONFIRM_ATTEMPTS` consecutive failed checks** (default 3 → ~15 min) — single network blips send nothing; once confirmed it alerts **once**, further failures stay silent, and recovery alerts **only if the outage had been confirmed**. Alerts go to `User.email` of active members of the groups registered in `NotificationRecipient`; group «Мониторинг» is auto-created (no perms). Emails are **text + styled HTML** (`_build_alert_content`, passed via `send_email_task(html_message=...)`) with service name, status, time, failure count, `Сбой с`, message and per-factory breakdown (`_diagnose` populates `details`). Old rows are pruned by `HEALTH_RETENTION_DAYS`. Public `GET /health/` returns 200/503 for external monitoring. Note reserve/burn emails (`app_cz/services/reserve_monitor.py`) still use `UIP_RESERVE_NOTIFICATION_EMAILS`, not the group mechanism.

## Product/SKU activity sync (`app_factory/services/product_activity_sync.py`, daily)

Pulls each factory's `product-list`, writes per-factory records into `ProductSKU.sku_links` JSON (`molvest_uuid` = our `Product.id`, `gtin`, `article`, `factory`, `active`), creates missing SKUs by article, and treats Molvest as the source of truth for `is_active` — **except** it never deactivates a SKU/product that has a live UIP (`reserved_cz`/`reserved_local`/`registered`); a product is active iff any of its SKUs is active. It also **pushes `ProductSKU.type_formation_uip` to Molvest** (`POST {factory}/workshop/api/v1/product-set-uip-type/` via `molvest_reference_sync.push_product_uip_type`) whenever `product-list` returns a different value; factory servers that don't return the field are skipped (nothing is overwritten). `python manage.py check_molvest_products` is a read-only reconciliation report. **Reverse case:** an external task requesting a UIP (`generate-uip` / `reserve-uips`) for an inactive product **reactivates** the SKU + product via `party_service.find_or_activate_sku_for_task` before reserving — the task wins, and the reserved UIP then protects them from the next activity sync.

## Admin & big tables

- **`CISCode`/`CISCodeArchive` admin must stay fast on huge tables.** `app_cz_ciscode` holds tens of millions of rows (a month's codes) and `app_cz_ciscodearchive` is never cleaned, so it's bigger. `CISCodeAdmin` orders by `-id` (PK index) — there is **no plain index on `CISCode.created_at`**, so ordering by `-created_at` sorts millions of rows per page load. Both admins use `LargeTablePaginator` (approximate count from `pg_class.reltuples` for the queryset's table **and DB alias**, so the archive's separate `archive` DB works) with `show_full_result_count=False`: Django's admin always calls `paginator.count` in `ChangeList.get_results`, so without it every changelist open runs a full `COUNT(*)`. `list_filter` on the choice fields is cheap (`ChoicesFieldListFilter`, no `DISTINCT`).

## UIP numbers & generation

- **UIP number format:** 14 GTIN digits + 6 date digits + 1-12 serial chars (regex-validated in the model).
- **Local UIP number building** (`app_cz/services/party_service.py::build_local_party_number`) branches on `ProductSKU.type_formation_uip`:
  - `general` = GTIN+date+`article[:5]` padded to 32;
  - `party_beginning` appends `party`;
  - `party_end` pads the base to 29 then right-justifies a 3-digit `party`;
  - `natura` («НатураПРО») = GTIN(14)+date(6)+`0000-`+**exactly 7-digit** internal party (a line-marker suffix like `2635798g` is stripped). Any non-7-digit Natura party returns `''` → `generate-uip` must 400.
  - The article is truncated to its first 5 chars for all non-`natura` types (e.g. `12345-1` → `12345`; a longer `article` is silently cut).
- **The format is duplicated in JS — keep both sides in sync.** `static/js/uip.js::buildLocalNumber` + `buildPreviewHint` mirror the server's `build_local_party_number`, and `updatePartyVisibility`/`validatePartyInput`/`submitGenerate` mirror the per-type party rules (Natura = exactly 7 digits, others ≤ 3). The live preview on `/uip/` is what users read, so a format change made only server-side silently shows (and submits) a wrong number — change the server branch **and** the JS together, and add/extend a server test in `app_cz/tests.py`. The display-side split (colored parts + legend) for `/search/` and the `/uip/` table is `app_cz/services/party_service.py::split_local_party_number`, exposed via the `uip_number_colored`/`uip_number_legend`/`uip_number_breakdown` tags in `app_page/templatetags/uip_display.py`. By **local convention** the 12 chars after the date are shown as «партия» (the article is encoded inside that block), i.e. `GTIN(14) + дата ГГММДД(6) + партия(12)`.
- **CZ generation (`mode='cz'`)** goes `generate_uip` → `_generate_cz_uip(product_sku, gtin, production_date)` (pass the **SKU object**, not the article string) → `generate_party_numbers`. TrueAPI `party-numbers/generate` needs `productionDate` as a **full ISO 8601 datetime** `%Y-%m-%dT%H:%M:%S.000Z` (date-only is rejected), `partyNumberInfo` items `{gtin, productionDate, count}`, and `pg` = `Product.group`.
- **Re-reservation of burned UIPs.** If an external use request arrives for a UIP that already exists in status `deleted` (burned after 30 days without registration), `_generate_local_uip` (deterministic local number) and `_reserve_own` (explicit `party_numbers`) re-reserve the **same number** in CZ via `reserve_parties_honest_sign`, then `_restore_burned_uip`/`restore_burned_uips` (`app_cz/services/party_service.py`) refresh `reservation_date`, clear `is_desync` and return the status to `reserved_local`. `skip_cz=True` and non-deleted existing UIPs keep the old "уже существует" behavior.
- **Status transitions** are audited in `UIPStatusLog` with source tracking (`admin`/`sync`/`api`/`service`).

## CI/CD & deployment

- **CI** (`.github/workflows/ci.yml`) runs on every push/PR with a Postgres 15 service: installs deps (iconv-converts the UTF-16 `requirements.txt`, strips `pywin32`/`git+`), then `manage.py check`, `makemigrations --check --dry-run` (non-blocking), `makemigrations`, `migrate` on **both** DBs (`--database archive` too), and `collectstatic`. Since migrations are gitignored, CI generates them from models — keep models and the tracked `config/migrations/0001_initial.py` consistent.
- **Deploy** (`.github/workflows/deploy.yml`) runs on push to `main` or manually on a **self-hosted runner** labeled `linux, party-management`, using repo variable `DEPLOY_PATH`; it just invokes `deploy.sh <branch>`.
- **Manual deploy scripts** `deploy.sh` (bash) / `deploy.ps1` (PowerShell): `git pull --ff-only` → `docker compose build` → wait for DB healthy → **gate** `check`/`makemigrations`/`collectstatic` (aborts on failure) → `migrate` both DBs → `docker compose up -d --build` → post-check. `--check-only`/`-CheckOnly` stops after the gate (no migrations/start). README «CI/CD» has the details.
- **Docker** (`docker-compose.yml`: web + Postgres 15) is available but not the primary dev flow. The container CMD is `makemigrations` → `migrate` **on both DBs** → `collectstatic` → `exec python run_all.py`; it does **not** run `reset_nk_sync` (run it manually to clear a stuck NK lock). The Dockerfile patches `settings.py` to take `DB_HOST` from env via `sed` and strips `pywin32`/`git+` lines from the UTF-16 requirements — don't mirror those hacks in local code.

## Other gotchas

- **Shutdown is graceful; `CancelledError` in logs is normal.** `run_all.py` handles SIGTERM/SIGINT (logs the signal, terminates all processes, waits up to 15 s before `kill`), uvicorn runs with `--timeout-graceful-shutdown 10`, and the Docker CMD ends with `exec python run_all.py` so container signals reach it. `config/asgi.py` installs a loop exception handler that turns `asyncio.shield`'s «CancelledError exception in shielded future» into one INFO line («Остановка сервера…») instead of a traceback. Many such lines = restarts/reloads (`DEBUG=1` enables `uvicorn --reload`), not a slow endpoint.

- **Versioning is tag-driven** (setuptools-scm). Commit subjects use `Feat:`/`Fix:`/`Docs:` prefixes; README maps `fix`→patch, `feat`→minor. Tag for a release: `git tag -a v2.0.0 -m "..."`. `config/_version.py` is auto-generated and gitignored; if it's missing after clone, run `pip install -e .`.
- **`helper_info/`** (untracked) holds the authoritative Chestny Znak API specs: `True_API_GIS_MT.txt` (True API + National Catalog methods, rate limits — source for `app_cz/services/rate_limit.py`) and `API_СУЗ_3.0.txt` (СУЗ-Облако). Consult these before changing CZ integration behavior instead of guessing from the official site.
- **Background tasks** use `django-tasks-db` with queues: `default`, `emails`, `high-priority`. Task functions are in `app_scheduler/tasks.py`.
- **External system URLs** (marking servers) are per-factory, stored in `Factory.ip_address` and `Factory.port_address`. Not in `.env`.
- **Live events** are written via `log_event()` from `app_event/utils.py` (module/level/message/actor/metadata); use it instead of ad-hoc logging for user-visible feed entries.
- **Management commands:** `app_cz/management/commands/archive_old_codes.py`, `app_cz/management/commands/reset_nk_sync.py`, `app_factory/management/commands/sync_molvest_reference.py`, `app_factory/management/commands/check_molvest_products.py`, plus `run_scheduler`/`run_tasks_worker` in `app_scheduler`.
- **`requirements.txt` is UTF-16LE-encoded** (Windows BOM `FF FE`), so Read/Edit tools see it as binary. The Dockerfile converts it via `iconv`; keep the encoding intact when editing.
- **`docs/`** is the tracked Russian documentation set (`01-overview`…`10-presentation-outline`, `CHANGELOG`); `.gitignore` does not exclude it. It is derived prose — `AGENTS.md`, `README.md`, and the code are the source of truth; treat specific `docs/` claims as unverified (e.g. `docs/CHANGELOG.md` still says `docs/` is gitignored, which is false).
