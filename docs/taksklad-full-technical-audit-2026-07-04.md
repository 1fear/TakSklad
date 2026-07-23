# Полный аудит проекта TakSklad

Дата аудита: 2026-07-04  
Рабочая директория: `/Users/anton/Documents/work/TakSklad`  
Git checkout: `main`, commit `c7b3ecf`  
Формат: source-code, docs, tests, config and local verification audit.

Ограничения аудита:

- Live production/VDS runtime не проверялся.
- GitHub Actions current status не проверялся.
- Секреты и реальные операционные данные не читались: `.env*`, credentials, runtime JSON, `outputs`, `backups`, `reports`, `exports`, `scan_backups`, реальные Excel/CSV/PDF/DOCX, рабочие отчеты и папка `Сверка`.
- Файл `docs/skladbot-api-key-functionality.md` помечен как `SENSITIVE_HISTORY`; целиком не цитировался.
- Worktree на момент аудита был dirty. Значит документ описывает текущий checkout с уже существующими локальными изменениями, а не чистый release snapshot.

Проверки, выполненные во время аудита:

| Проверка | Результат |
|---|---|
| `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests` | OK, `777` tests |
| `npm --prefix frontend run build` | OK, Vite build |
| `PYTHONPATH=. .venv/bin/python -m compileall -q backend/app backend/migrations tools tests src/taksklad main.py sitecustomize.py` | OK |
| `PYTHONPATH=. .venv/bin/python -m alembic -c backend/alembic.ini heads` | OK, head `20260701_0007` |
| `for script in deploy/vds/*.sh; do bash -n "$script"; done` | OK |
| `TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet` | OK |
| `.venv/bin/python tools/release_preflight.py --skip-network` | OK, network check skipped |

## 1. Executive Summary

TakSklad - складская система для процесса Excel-заказов, КИЗов, сканирования, Google Sheets mirror, Telegram-импорта, SkladBot-синхронизации, Smartup auto import и web/admin контроля.

Кому предназначен проект:

- складским операторам: сканирование КИЗов, печать, завершение заказов, возвраты;
- менеджерам и логистике: импорт Excel, даты отгрузки, логистические отчеты;
- техподдержке: диагностика, очереди, retry, инциденты, rollout;
- владельцу продукта: контроль активных заказов, SkladBot/Google/Smartup статусов.

Основные компоненты:

| Компонент | Роль |
|---|---|
| Desktop app | Python/Tkinter рабочее приложение склада |
| Backend API | FastAPI, Postgres, бизнес-логика, API contracts |
| Database | PostgreSQL как основной источник данных |
| Frontend | React/Vite web-admin panel |
| Workers | Telegram, Google Sheets sync, SkladBot, Smartup auto import |
| Deploy | Docker Compose, Traefik, Nginx, GitHub Actions |
| Tests/tools | unittest suite, release preflight, go/no-go, Windows helpers |

Текущий стек зрелый для внутренней складской системы: есть DB-first архитектура, миграции, очереди интеграций, audit log, readiness, CI, deploy/rollback runbooks и большое покрытие тестами. Это уже не простое Tkinter-приложение вокруг Google Sheets, а монорепо с desktop, backend, web-admin и production compose.

Главные сильные стороны:

- PostgreSQL стал source of truth, Google Sheets вынесен в mirror/fallback.
- КИЗы защищаются через отдельный ledger `kiz_codes`/`kiz_movements`.
- Для scan path есть backend locks, duplicate checks, wrong-SKU checks и undo/return lifecycle.
- Интеграции идут через очереди и retry, а не только через прямые синхронные вызовы.
- CI прогоняет Python tests, compileall, Alembic, shell syntax, compose config и frontend build.
- Deploy script делает restore point, fresh git sync, health/readiness checks и лог-сканирование.

Главные риски:

- Web cookie-auth не имеет явного CSRF token или Origin/Referer check для unsafe methods.
- Import API принимает `rows: list[dict[str, Any]]` без явных лимитов по количеству строк, размеру body и длинам полей.
- Service token дает роль `admin` и все permissions.
- Web/admin table, reports и часть import-dedup путей грузят крупные наборы данных в Python.
- Frontend `App.tsx` и несколько backend workers стали крупными монолитными файлами.
- В локальном checkout есть реальные секретные/runtime файлы и рабочие данные, они gitignored, но требуют дисциплины при deploy, audit и передаче агентам.
- Live production состояние в этом аудите не подтверждено.

Итог: проект выглядит рабочим и довольно зрелым, но для надежной production-grade эксплуатации ему нужны hardening web-auth, лимиты payload/rate, перенос тяжелых выборок в SQL, разбиение крупных UI/worker файлов и регулярный security/dependency audit.

## 2. Обзор репозитория

| Путь | Тип | Назначение | Комментарии |
|---|---|---|---|
| `README.md` | Документация | Главный вход в продукт, процесс и локальные команды | Актуальнее старых overview, но детали надо сверять с кодом |
| `README.txt` | Документация | Инструкция для Windows/onedir сборки | Не дубль `README.md` |
| `AGENTS.md` | Project instructions | Правила работы с repo, секретами, main branch и graph | Важен для AI-агентов |
| `main.py` | Python entrypoint | Локальный запуск desktop app | Добавляет `src` в path, поддерживает smoke flags |
| `pyinstaller_entry.py` | Python entrypoint | Entry для PyInstaller | Используется в Windows release flow |
| `src/taksklad/` | Desktop source | Tkinter app, scan flow, local queues, Google/backend bridge | Критическая часть склада |
| `taksklad/__init__.py` | Python package shim | Поддержка package/import contract | Небольшой compatibility layer |
| `backend/app/` | Backend source | FastAPI API, services, schemas, models, workers | Критическая server часть |
| `backend/migrations/` | DB migrations | Alembic migrations | Текущий head `20260701_0007` |
| `backend/sql/` | SQL bootstrap/history | Initial schema and KIZ movement history SQL | Используется Postgres init и historical bootstrap |
| `backend/README.md` | Backend docs | API/backend MVP, local Docker, auth notes | Использовать вместе с кодом |
| `backend/requirements.txt` | Dependency manifest | Python backend deps | FastAPI, SQLAlchemy, Alembic, psycopg, httpx |
| `frontend/` | Web app | React/Vite admin panel | Single SPA without router package |
| `frontend/src/App.tsx` | React source | Основная web panel | 3462 lines, главный maintainability hotspot |
| `frontend/src/api.ts` | React API client | Typed fetch wrapper and DTOs | 800 lines, central API layer |
| `frontend/src/styles.css` | CSS | Web UI styling | 2202 lines |
| `frontend/package.json` | Dependency manifest | Vite/React scripts and deps | Нет test script |
| `deploy/vds/` | Infra/deploy | Production Docker Compose and deploy scripts | Contains `.env.example`; real `.env` не читать |
| `deploy/traefik/` | Infra | Traefik stack | External reverse proxy network |
| `.github/workflows/ci.yml` | CI | Backend checks and frontend build | Хороший базовый gate |
| `.github/workflows/deploy-production.yml` | CD | Manual production deploy | Requires production secrets |
| `.github/workflows/build-windows-release.yml` | Release CI | Windows PyInstaller assets | Проверяет desktop smoke flags |
| `.githooks/` | Git hooks | Block commit/push outside `main` | Соответствует project branch discipline |
| `docs/` | Documentation | Runbooks, architecture, history, audits | Есть ACTIVE/HISTORY/SENSITIVE статусы |
| `tests/` | Tests | 59 unittest files | Локально `777` tests OK |
| `tools/` | Scripts | Release preflight, go/no-go, acceptance, Windows helpers | Важны для rollout |
| `assets/` | Static assets | Icons and product images | Используется desktop/release/UI |
| `version.json` | Release manifest | Current desktop version/forced rollout info | Version `2.0.25` |
| `telegram_settings.example.json` | Example config | Пример Telegram settings | Без реальных значений |
| `TakSklad.spec` | Build config | PyInstaller spec | Может быть legacy/current build artifact |
| `generated/` | Generated/reference assets | Mockups/templates/generated artifacts | Не анализировался построчно |
| `graphify-out/` | Generated graph output | Локальный graph artifact | Не source of truth |
| `.supergoal/` | Agent workflow state | История больших задач | Не продуктовый runtime |
| `.env*`, `credentials.json`, runtime JSON | Local sensitive/runtime files | Секреты и локальные данные | Не читались |
| `outputs`, `reports`, `exports`, `scan_backups`, `Сверка`, `отчеты` | Operational data | Рабочие выгрузки, отчеты, backup | Не читались как source |

Главное приложение фактически состоит из четырех runtime частей:

1. `src/taksklad` desktop.
2. `backend/app` API/services/workers.
3. `frontend` web panel.
4. `deploy/vds` production composition.

## 3. Технологический стек

| Область | Технология / библиотека | Где найдено | Зачем используется |
|---|---|---|---|
| Language | Python 3.12 | CI, venv, backend Docker | Desktop, backend, workers, scripts |
| Desktop UI | Tkinter | `src/taksklad/main.py`, tests | Рабочий интерфейс склада |
| Excel | `openpyxl`, `pandas` | `requirements.txt`, backend imports | Импорт/экспорт Excel, отчеты |
| Google Sheets | `gspread`, `oauth2client` | `requirements.txt`, `google_sheets_*` | Mirror, legacy fallback, exports |
| Images | `Pillow` | `requirements.txt`, assets/tests | Product images and desktop UI assets |
| Packaging | PyInstaller | `requirements.txt`, Windows workflow | Windows release executable/onedir |
| Backend framework | FastAPI | `backend/app/main.py`, backend deps | HTTP API |
| ASGI server | Uvicorn | `backend/Dockerfile`, backend deps | Production API server |
| Validation | Pydantic v2 | `backend/app/schemas.py` | DTO/request/response models |
| Database | PostgreSQL 16 | `deploy/vds/docker-compose.yml` | Main DB |
| ORM | SQLAlchemy 2 | `backend/app/models.py`, `db.py` | Models and DB access |
| Migrations | Alembic | `backend/migrations`, CI | Schema lifecycle |
| HTTP client | `httpx`, `urllib` | backend deps, desktop/backend client | External APIs and backend calls |
| Frontend | React 19 | `frontend/package.json` | Web admin panel |
| Frontend build | Vite 8, TypeScript | `frontend/package.json`, `vite.config.ts` | Build/dev server/typecheck |
| Icons | `lucide-react` | `frontend/package.json` | Web UI icons |
| Web server | Nginx | `frontend/Dockerfile`, `nginx.conf.template` | Static frontend and API proxy/auth_request |
| Containers | Docker Compose | `deploy/vds/docker-compose.yml` | Production services |
| Reverse proxy | Traefik | `deploy/traefik`, compose labels | HTTPS routing |
| CI/CD | GitHub Actions | `.github/workflows` | CI, deploy, Windows release |
| Tests | `unittest` | `tests/`, CI | Python test suite |
| Shell checks | `bash -n` | CI and local run | Deploy script syntax |
| External WMS | SkladBot API | `skladbot_*` modules | Requests, matching, returns, reports |
| External CRM/ERP | Smartup API | `smartup_auto_import.py` | Auto import terminal orders |
| Messaging | Telegram Bot API | `telegram_worker.py`, desktop telegram modules | Excel ingest, reports, commands |
| Geocoding | Yandex geocoder key | compose env, Smartup/geocoding tests | Coordinates for logistics |

Dependency freshness against PyPI/npm registries was not checked in this audit. Therefore no claim is made that versions are latest or outdated.

## 4. Как запустить проект

### Python/Desktop local

Based on `README.md`, CI and current repo structure:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r backend/requirements.txt
.venv/bin/python main.py
```

Alternative module runner:

```bash
tools/run_desktop_local.sh
```

Smoke flags:

```bash
.venv/bin/python main.py --smoke-import
.venv/bin/python main.py --smoke-gui
.venv/bin/python pyinstaller_entry.py --smoke-import
.venv/bin/python pyinstaller_entry.py --smoke-gui
```

### Backend local

Direct API dev command inferred from `backend/Dockerfile` and FastAPI layout:

```bash
cd backend
PYTHONPATH=. uvicorn app.main:app --reload
```

For local DB Docker setup, use documented VDS compose path with example env only:

```bash
cp deploy/vds/.env.example deploy/vds/.env
TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml up -d --build
curl http://127.0.0.1:8000/health
```

Do not use real `deploy/vds/.env` values in docs, logs or agents.

### Frontend local

From `frontend/package.json`:

```bash
npm ci --prefix frontend
npm --prefix frontend run dev
npm --prefix frontend run build
npm --prefix frontend run preview
```

### Tests and quality checks

Confirmed working locally:

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
PYTHONPATH=. .venv/bin/python -m compileall -q backend/app backend/migrations tools tests src/taksklad main.py sitecustomize.py
PYTHONPATH=. .venv/bin/python -m alembic -c backend/alembic.ini heads
for script in deploy/vds/*.sh; do bash -n "$script"; done
TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet
npm --prefix frontend run build
.venv/bin/python tools/release_preflight.py --skip-network
```

### Migrations

Alembic source:

```bash
PYTHONPATH=. .venv/bin/python -m alembic -c backend/alembic.ini heads
PYTHONPATH=. .venv/bin/python -m alembic -c backend/alembic.ini upgrade head
```

Production migration/rollback posture is documented in `docs/database-migrations-runbook.md`. Rollback is backup/restore or forward migration, not automatic downgrade-first workflow.

### Windows release

Windows release flow is represented by:

- `.github/workflows/build-windows-release.yml`
- `tools/build_windows_test_archive.ps1`
- `README.txt`
- `version.json`
- `tools/release_preflight.py`

## 5. Архитектура приложения

TakSklad is a monorepo with a DB-first warehouse runtime. It is not a microservice system in the strict sense, because services share one codebase and one DB schema, but production runtime is split into multiple containers and workers.

High-level model:

```text
[Складской оператор Windows]
        |
        v
[Tkinter Desktop App]
        |
        +------------------------+
        |                        |
        v                        v
[FastAPI Backend API]       [Google Sheets fallback/mirror]
        |
        v
[PostgreSQL Source of Truth]
        |
        +--> [Pending Events Queue]
        |          |
        |          +--> [Google Sheets Sync Worker]
        |          +--> [SkladBot Worker]
        |          +--> [Telegram Worker]
        |          +--> [Smartup Auto Import Worker]
        |
        +--> [Audit Log / Incidents / Readiness]

[Browser User]
        |
        v
[React/Vite Admin Panel]
        |
        v
[Nginx auth_request proxy]
        |
        v
[FastAPI Backend API]

[Traefik HTTPS]
        |
        +--> backend-api
        +--> frontend
```

Runtime split:

- Desktop handles the warehouse scanner/operator workflow and local offline-ish queues.
- Backend owns persistence, validation, KIZ lifecycle, imports, reports, admin actions and integration queues.
- Workers handle external services and scheduled/background work.
- Frontend gives admin/operator visibility and controlled manual actions.
- Postgres is the source of truth.
- Google Sheets is mirror/export/fallback, not the primary DB.

Important architectural decisions visible in code:

- `scan_codes.code` is not globally unique because returned KIZ can be reused.
- `kiz_codes.code` is unique and `kiz_movements` tracks lifecycle.
- Backend scan path uses DB row lock and PostgreSQL advisory transaction lock per KIZ.
- Google export happens after DB commit through queue, so DB can be correct while mirror is temporarily stale.
- Web frontend is protected through session cookie and Nginx `auth_request`; service token still exists for trusted clients/workers.

## 6. Точки входа и execution flow

| Точка входа | Файл | Что запускает | Зависимости / куда ведет дальше |
|---|---|---|---|
| Desktop local | `main.py` | `taksklad.main.run_app()` or smoke modes | `src/taksklad`, local config, backend/Google |
| Desktop package | `pyinstaller_entry.py` | Packaged desktop app | PyInstaller, `src/taksklad/main.py` |
| Desktop app class | `src/taksklad/main.py` | `ScanningApp` Tk root and mixins | layout, scanning, imports, returns, updates |
| Backend API | `backend/app/main.py` | FastAPI `app` | settings, db, services, schemas |
| Backend Docker | `backend/Dockerfile` | `uvicorn app.main:app` | backend requirements |
| Telegram worker | `backend/app/telegram_worker.py` | Polling/import/report commands | Telegram API, backend API, DB |
| Google worker | `backend/app/google_sheets_sync_worker.py` | Pending Google export processing | Postgres, Google Sheets |
| SkladBot worker | `backend/app/skladbot_worker.py` | SkladBot sync/matching | SkladBot API, DB |
| Smartup worker | `backend/app/smartup_auto_import_worker.py` | Scheduled Smartup auto import | Smartup API, DB, SkladBot, Telegram |
| Frontend app | `frontend/src/App.tsx` | React SPA | `api.ts`, CSS, browser session |
| Frontend build | `frontend/package.json` | `tsc -b && vite build` | TypeScript/Vite |
| Production compose | `deploy/vds/docker-compose.yml` | postgres, backend, frontend, workers | Docker, Traefik network, env |
| Production deploy | `deploy/vds/deploy_from_git.sh` | Backup, sync ref, compose deploy, checks | Git, Docker, curl |
| CI | `.github/workflows/ci.yml` | Python checks and frontend build | GitHub Actions |
| Release preflight | `tools/release_preflight.py` | Local release readiness checks | version manifest, acceptance kit, tracked secrets |
| Go/no-go | `tools/release_go_no_go.py` | Acceptance verdict parser | `outputs/taksklad_acceptance` when allowed |

Most important runtime flows:

1. Excel import:
   user sends/upload Excel -> normalizer parses rows -> backend `create_import()` writes orders/items/import job -> queues Google mirror -> creates SkladBot dry-run/request queue.
2. Scan KIZ:
   operator scans in desktop -> desktop validates format and local state -> backend `/api/v1/scans` locks item and KIZ -> writes `scan_codes` and `kiz_movements` -> queues Google update -> desktop updates UI/local backup.
3. Return:
   operator/admin looks up completed order -> backend marks returned -> writes return movements for KIZs -> queues SkladBot return and Google archive/mirror update.
4. Web admin action:
   browser session -> Nginx auth check -> frontend API call -> backend permission check -> action service writes audit and queue events.
5. Smartup auto import:
   worker selects eligible slot/orders -> preview/audit -> backend import -> Smartup status change -> queue SkladBot only for successful status changes -> Telegram/logistics outputs.

## 7. Анализ модулей

### Модуль: `src/taksklad`

- Путь: `src/taksklad/`
- Ответственность: Windows desktop app for warehouse operations.
- Ключевые файлы: `main.py`, `app_layout.py`, `app_scanning.py`, `app_returns.py`, `app_imports.py`, `app_data_loading.py`, `app_runtime.py`, `backend_client.py`, `backend_events.py`, `storage.py`, `startup_check.py`, `update_service.py`.
- Ключевые классы/функции: `ScanningApp`, `run_app()`, `create_scan() client bridge`, pending backend queue functions, storage backup helpers.
- Входные данные: scanner input, Excel files, local JSON queues, backend orders, Google rows fallback.
- Выходные данные: backend scans/completions/returns, Google updates through backend or fallback, local backups, reports, UI state.
- Зависимости: Tkinter, openpyxl/pandas, gspread, backend API, local filesystem.
- Где используется: local desktop launch and PyInstaller release.
- Комментарии/риски: critical warehouse UI. Reliability work exists, but desktop remains stateful and depends on backend/Google/network conditions.

### Модуль: `backend/app/main.py`

- Путь: `backend/app/main.py`
- Ответственность: FastAPI app, route registration, auth dependencies, session endpoints, API handlers.
- Ключевые файлы: `main.py`, `schemas.py`, `settings.py`, `db.py`.
- Ключевые классы/функции: `read_auth_context()`, `require_permission()`, `/health`, `/ready`, API routers.
- Входные данные: HTTP requests, cookies, Bearer token, JSON bodies.
- Выходные данные: typed Pydantic responses, HTTP errors, DB mutations.
- Зависимости: FastAPI, SQLAlchemy session dependency, services.
- Где используется: backend-api container and frontend proxy.
- Комментарии/риски: service token maps to admin; cookie-auth lacks explicit CSRF protection.

### Модуль: `backend/app/models.py`

- Путь: `backend/app/models.py`
- Ответственность: SQLAlchemy ORM model layer.
- Ключевые сущности: `Order`, `OrderItem`, `ScanCode`, `KizCode`, `KizMovement`, `ImportJob`, `ImportFile`, `PendingEvent`, `Incident`, `ClientPoint`, `LogisticsCalendarDay`, `RepresentativeContact`, `User`, `AuditLog`.
- Входные данные: service writes and queries.
- Выходные данные: persisted domain state.
- Зависимости: SQLAlchemy, PostgreSQL JSONB variant.
- Комментарии/риски: good explicit indexes on events/incidents/KIZ movement; some high-volume queries still full-load rows into Python.

### Модуль: `backend/app/orders_service.py`

- Путь: `backend/app/orders_service.py`
- Ответственность: active orders, scans, undo, complete, returns, KIZ availability.
- Ключевые функции: `list_active_orders()`, `lookup_kiz_availability()`, `create_scan()`, `undo_scan()`, `complete_order()`, `mark_order_returned()`.
- Входные данные: API DTOs and DB state.
- Выходные данные: `ScanCode`, `KizMovement`, status updates, audit logs, pending Google events.
- Зависимости: `kiz_movements_service`, `google_sheets_pending`, product/scan quantity helpers.
- Комментарии/риски: central warehouse integrity module. It correctly uses locks, but correctness relies on service code plus movement ledger rather than a global unique constraint on `scan_codes.code`.

### Модуль: `backend/app/imports_service.py`

- Путь: `backend/app/imports_service.py`
- Ответственность: backend import of normalized rows into orders/items/imports.
- Ключевые функции: `create_import()`, `load_existing_import_keys()`, row normalization, Google export enqueue, SkladBot dry-run creation.
- Входные данные: `ImportCreate.rows`.
- Выходные данные: `ImportJob`, `ImportFile`, `Order`, `OrderItem`, pending events.
- Зависимости: SQLAlchemy, Google pending queue, SkladBot dry-run, client points.
- Комментарии/риски: import payload is flexible but currently too open; dedup loads existing orders/items into memory.

### Модуль: `backend/app/google_sheets_pending.py` and exporters

- Путь: `backend/app/google_sheets_pending.py`, `backend/app/google_sheets_exporter.py`, `backend/app/google_sheets_sync_worker.py`
- Ответственность: queue and process Google Sheets mirror writes.
- Входные данные: pending events from import/scan/archive/return actions.
- Выходные данные: Google Sheets updates and event status.
- Зависимости: gspread, Postgres events.
- Комментарии/риски: good queue-based decoupling; mirror lag is expected and must be visible in operations/readiness.

### Модуль: `backend/app/skladbot_*`

- Путь: `backend/app/skladbot_worker.py`, `skladbot_request_dry_run.py`, `skladbot_return_requests.py`, `skladbot_daily_report.py`, diagnostics modules.
- Ответственность: SkladBot matching, request create dry-run/processing, returns, daily reports and diagnostics.
- Входные данные: orders, pending events, SkladBot API.
- Выходные данные: request numbers/statuses, audit payloads, pending event results.
- Зависимости: SkladBot API token(s), backend DB.
- Комментарии/риски: rate limits/retries exist, but business matching can remain fragile; address soft match is diagnostic, not necessarily a hard blocker.

### Модуль: `backend/app/telegram_worker.py`

- Путь: `backend/app/telegram_worker.py`
- Ответственность: Telegram polling, Excel document handling, date prompt, admin commands, reports.
- Входные данные: Telegram updates/files.
- Выходные данные: backend imports, messages, pending events, reports.
- Зависимости: Telegram Bot API, backend API, DB.
- Комментарии/риски: large file, high operational value. Admin allowlist is fail-open when `TELEGRAM_ADMIN_CHAT_IDS` is empty.

### Модуль: `backend/app/smartup_auto_import.py`

- Путь: `backend/app/smartup_auto_import.py`
- Ответственность: scheduled Smartup terminal import, previews, status change, SkladBot queue, client/logistics reporting.
- Входные данные: Smartup orders, schedule/env config.
- Выходные данные: backend imports, Smartup status change, SkladBot events, audit/logistics outputs.
- Зависимости: Smartup API, SkladBot, Telegram, DB, outputs folder.
- Комментарии/риски: backend import happens before Smartup status change. If status change fails, backend can already contain imported data and recovery must use audit/history.

### Модуль: `backend/app/health_service.py`, `operations_service.py`, `event_queue_service.py`, `incidents_service.py`

- Путь: `backend/app/*health*`, `operations_service.py`, `event_queue_service.py`, `incidents_service.py`
- Ответственность: readiness, queue diagnostics, operator attention, incidents.
- Входные данные: DB, pending events, incidents, imports, settings.
- Выходные данные: `/ready`, `/api/v1/readiness`, admin operations panels.
- Комментарии/риски: strong operational observability base. Needs alerting/SLOs if production load grows.

### Модуль: `frontend`

- Путь: `frontend/`
- Ответственность: web-admin panel.
- Ключевые файлы: `src/App.tsx`, `src/api.ts`, `src/styles.css`, `nginx.conf.template`, `Dockerfile`.
- Ключевые компоненты: `LoginScreen`, admin table, calendar panel, clients panel, Smartup panel, dry-run panel, incidents/events/activity/diagnostics panels.
- Входные данные: backend API responses, session cookie.
- Выходные данные: admin actions and UI state.
- Зависимости: React, Vite, TypeScript, lucide-react.
- Комментарии/риски: build passes, but source organization is monolithic and has no dedicated frontend tests.

### Модуль: `deploy`

- Путь: `deploy/vds`, `deploy/traefik`
- Ответственность: production compose, reverse proxy, deploy/acceptance scripts.
- Ключевые файлы: `docker-compose.yml`, `deploy_from_git.sh`, `.env.example`, Traefik compose.
- Входные данные: env variables and Git ref.
- Выходные данные: running containers, restore points, health/readiness checks.
- Комментарии/риски: good deploy hygiene; live state not checked here.

### Модуль: `tools`

- Путь: `tools/`
- Ответственность: release, acceptance, representative contacts import, reconciliation helpers, Windows acceptance/build helpers.
- Ключевые файлы: `release_preflight.py`, `release_go_no_go.py`, `build_windows_test_archive.ps1`, `windows_backend_acceptance.ps1`, `import_representative_contacts.py`.
- Комментарии/риски: release tools are important source of truth for rollout discipline.

### Модуль: `tests`

- Путь: `tests/`
- Ответственность: Python unittest coverage for backend, desktop, integrations, release tooling.
- Текущее состояние: 59 `test*.py` files, local run `777` tests OK.
- Комментарии/риски: frontend has build/typecheck, but no test script or e2e tests found.

## 8. Frontend-анализ

Frontend exists and is a React/Vite/TypeScript SPA. It does not use a router package. Navigation is implemented through local tab state:

`type Tab = "table" | "calendar" | "clients" | "smartup" | "imports" | "skladbotDryRun" | "incidents" | "activity"`.

### Routes / pages

| Route / Page | Файл | Назначение | Какие данные нужны | Комментарии |
|---|---|---|---|---|
| Login | `frontend/src/App.tsx` | Phone/password login | auth session/login API | Session cookie, password field |
| Loading gate | `frontend/src/App.tsx` | Initial auth/session loading | `/api/v1/auth/session` | Basic loading state |
| `table` | `frontend/src/App.tsx` | Admin table of orders/items | admin table, dashboard, filters | Main operations surface |
| `calendar` | `frontend/src/App.tsx` | Logistics calendar | logistics calendar API | Admin write needed for edits |
| `clients` | `frontend/src/App.tsx` | Client points and timeslots | client points, order summary | Role/permission sensitive |
| `smartup` | `frontend/src/App.tsx` | Smartup auto import history | Smartup history API | Shows import/audit status |
| `imports` | `frontend/src/App.tsx` | Import list and context | import API | Operational history |
| `skladbotDryRun` | `frontend/src/App.tsx` | SkladBot dry runs | dry-run API | Can rebuild/resync through admin action |
| `incidents` | `frontend/src/App.tsx` | Incidents and event queue | incidents/events/operations APIs | Manual review and retry surface |
| `activity` | `frontend/src/App.tsx` | Diagnostics and activity list | readiness, operations, activity | Observability surface |

### Major components

| Component | Файл | Назначение | Props / Inputs | Где используется |
|---|---|---|---|---|
| `App` | `frontend/src/App.tsx` | Main SPA state and routing | API config/session/state | Root |
| `LoginScreen` | `frontend/src/App.tsx` | Login form | phone/password/error/loading callbacks | Unauthenticated state |
| `LoadingGate` | `frontend/src/App.tsx` | Loading shell | none | Session load |
| `DataTable` | `frontend/src/App.tsx` | Admin table | rows, filters, action state | `table` tab |
| `ClientsPanel` | `frontend/src/App.tsx` | Client points management | client points/order history | `clients` tab |
| `LogisticsCalendarPanel` | `frontend/src/App.tsx` | Calendar UI | days, save handler | `calendar` tab |
| `SkladBotDryRunPanel` | `frontend/src/App.tsx` | Dry-run list/actions | dry runs, rebuild handler | `skladbotDryRun` tab |
| `SmartupAutoImportPanel` | `frontend/src/App.tsx` | Smartup run history | history payloads | `smartup` tab |
| `AdminCenterPanel` | `frontend/src/App.tsx` | Events/incidents | queue, incidents, operations | `incidents` tab |
| `SystemDiagnosticsPanel` | `frontend/src/App.tsx` | Readiness/operations summary | readiness, queues | `activity` tab |
| `SelectFilter` | `frontend/src/App.tsx` | Filter control | value/options | Multiple panels |

### State management

State is local React state in `App.tsx`: session, active tab, filters, selected rows, admin table, incidents, events, readiness, operations, Smartup history, logistics calendar and client points. There is no Redux/Zustand/query library.

### API communication

`frontend/src/api.ts` is a central typed client. It:

- uses same-origin default API URL (`defaultApiUrl()` returns `""`);
- sends `credentials: "include"`;
- optionally adds Bearer token if config token exists;
- has default and long request timeout constants;
- serializes JSON request bodies;
- maps backend DTOs into TypeScript types.

Production Nginx strips browser `Authorization` header and routes `/api/` through `auth_request`.

### Forms and validation

Validation is mostly local and backend-backed:

- login normalizes phone input and sends password to backend auth;
- filters are controlled state;
- admin actions use confirmations, reasons and `expected_updated_at` for conflict checks;
- backend remains final validation layer.

### Styling and assets

- CSS is plain `frontend/src/styles.css`.
- Logo asset: `frontend/public/taksklad.png`.
- Icons: `lucide-react`.
- No design system package found.

### Error/loading states

There are loading gates, panel loading states, notices and error messages. A notable UX risk is `refreshPanelContext` behavior that can ignore side-panel refresh errors while main table refresh continues.

### Accessibility concerns

Confirmed concerns:

- Some table rows use interactive behavior through row role/tabIndex instead of native buttons.
- `SelectFilter` uses native `<select>`, which is functional but inconsistent with a custom web control approach.
- Desktop custom `AppButton` uses custom Frame/Canvas and `takefocus=0`, which is weaker for keyboard users than native buttons.

### Performance concerns

- `App.tsx`: 3462 lines.
- `styles.css`: 2202 lines.
- `api.ts`: 800 lines.
- Build output JS is about 289 KB raw, 84 KB gzip from current build.
- No route-based splitting or e2e tests found.

## 9. Backend-анализ

Backend is FastAPI with SQLAlchemy sessions and Pydantic schemas. Public endpoints are `/health` and `/ready`; protected endpoints are under `/api/v1`.

Auth model:

- Bearer service token if `TAKSKLAD_API_TOKEN` is configured.
- Web session cookie if web auth is enabled.
- Local dev admin fallback only when neither API auth nor web auth is enabled.
- Role permissions come from `backend/app/web_auth.py`.

### Endpoint table

| Endpoint / Route | Method | Handler file | Назначение | Требует auth? | Inputs | Outputs |
|---|---|---|---|---|---|---|
| `/health` | GET | `backend/app/main.py` | Liveness | No | none | `HealthResponse` |
| `/ready` | GET | `backend/app/main.py` | Readiness | No | DB dependency | `ReadinessResponse` |
| `/api/v1/auth/login` | POST | `backend/app/main.py` | Web login | No | `AuthLoginRequest` | `AuthSessionRead` + cookie |
| `/api/v1/auth/logout` | POST | `backend/app/main.py` | Logout | Cookie optional | cookie | cleared cookie/session response |
| `/api/v1/auth/session` | GET | `backend/app/main.py` | Current session | Cookie | cookie | `AuthSessionRead` |
| `/api/v1/auth/check` | GET | `backend/app/main.py` | Nginx auth_request check | Cookie | cookie | status |
| `/api/v1/orders/active` | GET | `backend/app/main.py` | Active orders for desktop/web | Service token or session | filters implicit | `list[OrderRead]` |
| `/api/v1/admin/table` | GET | `backend/app/main.py` | Main admin table | Auth | filters, pagination | `AdminTableRead` |
| `/api/v1/admin/dashboard/day-summary` | GET | `backend/app/main.py` | Dashboard daily summary | Auth | date | `DashboardDaySummaryRead` |
| `/api/v1/admin/client-points` | GET/POST | `backend/app/main.py` | Client point list/create/update flows | GET auth, writes need permission | client/address/timeslot | client point DTOs |
| `/api/v1/admin/logistics-calendar` | GET/POST | `backend/app/main.py` | Logistics calendar | Auth/write permission | date/day payload | calendar DTOs |
| `/api/v1/admin/google/pending/retry` | POST | `backend/app/main.py` | Retry Google pending exports | Admin write | event/reason | action result |
| `/api/v1/admin/events` | GET | `backend/app/main.py` | Event queue diagnostics | Auth | filters | `EventQueueDiagnosticsRead` |
| `/api/v1/admin/events/{event_id}` | GET/POST | `backend/app/main.py` | Event detail/retry | Auth/admin write | event id, reason | event DTO |
| `/api/v1/admin/operations` | GET | `backend/app/main.py` | Operations attention | Auth | none | `OperationsAttentionRead` |
| `/api/v1/admin/incidents` | GET/POST | `backend/app/main.py` | Incident list/create/status | Auth/admin write | status payloads | incident DTOs |
| `/api/v1/admin/smartup-auto-imports/history` | GET | `backend/app/main.py` | Smartup run history | Auth | filters | history DTO |
| `/api/v1/readiness` | GET | `backend/app/main.py` | Authenticated readiness | Auth | DB | `ReadinessResponse` |
| `/api/v1/admin/orders/...` | POST | `backend/app/main.py` | Archive/cancel/delete/reset/restore/resync actions | Admin write | order/action payload | order/action DTO |
| `/api/v1/admin/skladbot/dry-runs` | GET/POST | `backend/app/main.py` | Dry-run list/rebuild | Auth/admin write | import id/action | dry-run DTOs |
| `/api/v1/sync/sources` | POST | `backend/app/main.py` | Trigger sync sources | Admin write | flags | sync result |
| `/api/v1/returns` | GET | `backend/app/main.py` | Returns list | Auth | filters | `list[OrderRead]` |
| `/api/v1/scans` | POST | `backend/app/main.py` | Create scan | Auth | `ScanCreate` | `ScanRead` |
| `/api/v1/kiz/availability` | GET | `backend/app/main.py` | Read-only KIZ availability | Auth | code, order_item_id | `KizAvailabilityRead` |
| `/api/v1/scans/undo` | POST | `backend/app/main.py` | Undo scan | Admin write | scan/order item payload | `ScanRead` |
| `/api/v1/orders/{order_id}/complete` | POST | `backend/app/main.py` | Complete order | Admin write | order id | `OrderRead` |
| `/api/v1/returns/lookup` | GET | `backend/app/main.py` | Lookup completed order for return | Auth | query | `OrderRead` |
| `/api/v1/returns/{order_id}` | POST | `backend/app/main.py` | Mark returned | Admin write | order id, confirmation | `OrderRead` |
| `/api/v1/imports` | GET/POST | `backend/app/main.py` | Import list/create | Auth/admin write for create | `ImportCreate` | import DTOs |
| `/api/v1/imports/preview` | POST | `backend/app/main.py` | Import preview | Admin write | upload/rows | preview DTO |
| `/api/v1/reports/day` | GET | `backend/app/main.py` | Daily report | Auth | date | `DayReportRead` |
| `/api/v1/reports/reconciliation/day` | GET | `backend/app/main.py` | Reconciliation report | Admin write | date | report payload |
| `/api/v1/reports/kiz/*` | GET | `backend/app/main.py` | KIZ source/date/range reports | Auth | date/source params | report files/data |
| `/api/v1/logistics/dates` | GET | `backend/app/main.py` | Logistics date options | Auth | range | dates |
| `/api/v1/logistics/report` | GET | `backend/app/main.py` | Logistics report | Auth | date | report |
| `/api/v1/diagnostics/logs` | GET | `backend/app/main.py` | Diagnostics log download | Auth | params | file/response |

### Middleware

- CORS is added only when `TAKSKLAD_CORS_ORIGINS` is set.
- `allow_credentials=False`, allowed methods `GET`, `POST`, `OPTIONS`, headers `Authorization`, `Content-Type`.

### Validation

- Pydantic schemas validate request/response DTOs.
- `ScanCreate` rejects whitespace/newline in KIZ code.
- Import rows are flexible `dict[str, Any]`, then normalized in service code.

### Error handling

- Services raise `ApiError` or FastAPI `HTTPException`.
- Queue/worker paths often catch exceptions, record pending event status or incident, and keep processing.
- Redaction module exists for secrets and KIZ-like codes in diagnostics/logs.

### Background processing

Workers:

- `telegram-worker`
- `google-sheets-sync-worker`
- `skladbot-worker`
- `smartup-auto-import-worker`

All are built from backend image and share DB/env.

## 10. База данных и модель данных

Database: PostgreSQL. ORM: SQLAlchemy 2. Migrations: Alembic.

### Entities

| Entity / Table | Файл / определение | Поля | Связи | Назначение |
|---|---|---|---|---|
| `orders` | `backend/app/models.py` `Order` | source, external_id, order_date, payment_type, client, address, representative, status, raw_payload | one-to-many `order_items` | Заказ/группа отгрузки |
| `order_items` | `OrderItem` | product, quantities, scanned_blocks, requires_kiz, status, raw_payload | belongs to order, has scan codes | Строка товара в заказе |
| `scan_codes` | `ScanCode` | code, source, workstation_id, scanned_by, scanned_at, raw_payload | belongs to order item | Факт сканирования |
| `kiz_codes` | `KizCode` | unique code, first_seen_at, updated_at | has movements | Уникальный реестр КИЗ |
| `kiz_movements` | `KizMovement` | movement_type, order refs, scan ref, return_reference, source, actor | belongs to KIZ | Ledger движения КИЗ |
| `imports` | `ImportJob` | source, status, rows_total/imported, raw_payload | referenced by files/incidents | Импортная операция |
| `import_files` | `ImportFile` | filename, sha256 unique, size_bytes | optional import ref | Dedup по файлу |
| `pending_events` | `PendingEvent` | event_type, idempotency_key, status, attempts, payload, last_error | referenced by incidents | Очередь интеграций |
| `incidents` | `Incident` | source, severity, status, title, entity refs, raw_payload | optional refs to event/order/item/import/scan | Операционные проблемы |
| `client_points` | `ClientPoint` | client/address normalized, coordinates, representative, delivery times | unique normalized client/address | Справочник точек клиента |
| `logistics_calendar_days` | `LogisticsCalendarDay` | service_date, is_non_working, reason | unique service date | Календарь логистики |
| `representative_contacts` | `RepresentativeContact` | name, normalized_name, work_phone, personal_phone, work_zone | unique normalized name | Контакты торговых представителей |
| `users` | `User` | username, password_hash, role, is_active | audit actor optional | Web/admin users |
| `audit_log` | `AuditLog` | actor_user_id, action, entity_type/id, payload | optional user FK | Audit trail |

### Relationships

| Связь | Описание | Доказательство в коде |
|---|---|---|
| Order -> OrderItem | Заказ содержит позиции | `Order.items` relationship |
| OrderItem -> ScanCode | Позиция содержит сканы | `OrderItem.scan_codes` relationship |
| KizCode -> KizMovement | КИЗ имеет историю движений | `KizCode.movements` relationship |
| PendingEvent -> Incident | Инцидент может ссылаться на очередь | `Incident.pending_event_id` FK |
| ImportFile -> ImportJob | Файл может ссылаться на импорт | `ImportFile.import_id` FK |
| AuditLog -> User | Audit может ссылаться на пользователя | `AuditLog.actor_user_id` FK |

### Indexes and constraints

Important confirmed constraints:

- `kiz_codes.code` unique.
- `scan_codes.code` indexed, not unique.
- `scan_codes(code, order_item_id)` indexed.
- `pending_events.idempotency_key` unique index.
- `import_files.sha256` unique.
- `client_points(normalized_client, normalized_address)` unique.
- `logistics_calendar_days.service_date` unique.
- Incidents have indexes by status/severity/source/entity/refs.

Lifecycle of key entities:

1. Import creates `ImportJob`, optional `ImportFile`, `Order`, `OrderItem`.
2. Scan creates `ScanCode` and `KizMovement(outbound|re_outbound)`.
3. Undo writes `KizMovement(undo)` and removes/recomputes scan state.
4. Return writes `KizMovement(return)` for all related scans and makes KIZ available again.
5. Async integrations create/update `PendingEvent`; failures may create `Incident`.

## 11. API-контракты и движение данных

DTO layer lives mainly in `backend/app/schemas.py` and `frontend/src/api.ts`.

Key request/response shapes:

- `AuthLoginRequest`: login/password.
- `AuthSessionRead`: authenticated flag, login, role, permissions, expiry.
- `OrderRead`: order fields plus nested items.
- `OrderItemRead`: product, quantities, scanned state, scan codes.
- `ScanCreate`: order item id, KIZ code, workstation/scanned_by/raw payload.
- `ScanRead`: scan id, code, scanned blocks, item status, scan metadata.
- `KizAvailabilityRead`: code, available, reason, latest movement info.
- `ImportCreate`: source, filename, sha256, Telegram IDs, rows.
- `ImportResult`: import counts, duplicate/invalid info, errors.
- `AdminTableRead`: totals, rows, activity, pagination fields.
- `Incident*`, `EventQueue*`, `OperationsAttention*`, `ClientPoint*`, `LogisticsCalendar*`, `SmartupAutoImport*`.

### Flow: Excel import

```text
Telegram/Desktop upload
 -> Excel parser/normalizer
 -> ImportCreate rows
 -> POST /api/v1/imports or Telegram backend call
 -> imports_service.create_import()
 -> load_existing_import_keys()
 -> create/update Order and OrderItem
 -> ImportJob/ImportFile/AuditLog
 -> queue_google_sheets_export()
 -> create_skladbot_dry_run_for_import()
 -> response with counts/errors
```

Failure modes:

- invalid row skipped;
- duplicate row skipped;
- Google export queue failure recorded;
- SkladBot dry-run failure logged but import can still exist;
- large input can stress memory/DB because request shape is flexible.

### Flow: KIZ scan

```text
Scanner input
 -> Desktop app_scanning
 -> backend_client.create_scan()
 -> POST /api/v1/scans
 -> orders_service.create_scan()
 -> SELECT OrderItem FOR UPDATE
 -> pg_advisory_xact_lock(KIZ)
 -> duplicate and movement checks
 -> product/aggregate SKU checks
 -> insert ScanCode
 -> insert KizMovement
 -> update scanned_blocks/item status
 -> AuditLog
 -> queue Google mirror
 -> desktop updates UI/local backup
```

Failure modes:

- duplicate in another active item;
- KIZ not available because latest movement is not return/undo/reset;
- wrong SKU;
- aggregate box exceeds remaining blocks;
- item/order already complete;
- backend unavailable, desktop pending backend queue handles retry/blocked events.

### Flow: Return

```text
Return lookup
 -> GET /api/v1/returns/lookup
 -> operator confirms
 -> POST /api/v1/returns/{order_id}
 -> mark_order_returned()
 -> status=returned
 -> KizMovement(return) for scans
 -> SkladBot return request pending event
 -> Google archive/return export pending event
```

Important rule: KIZ can be reused for outbound only after latest movement is `return`, `undo` or `reset`.

### Flow: Web admin action

```text
React tab/action
 -> api.ts fetch(credentials=include)
 -> Nginx /api auth_request /api/v1/auth/check
 -> FastAPI read_auth_context()
 -> permission dependency
 -> order_actions_service/admin service
 -> AuditLog/PendingEvent/Order mutation
 -> response
 -> UI refresh
```

Failure modes:

- expired session;
- insufficient permissions;
- optimistic conflict by `expected_updated_at`;
- backend/service validation rejects action;
- side-panel refresh can silently miss some context errors.

### Flow: Smartup auto import

```text
Smartup worker schedule
 -> select orders for slot
 -> build preview and audit
 -> create backend delivery-group imports
 -> Smartup change_status()
 -> queue SkladBot only for successful deal ids
 -> send client export/logistics report
 -> write Smartup history/audit
```

Failure mode: backend import happens before Smartup status change. If status change fails, the backend import is already present and needs recovery/retry handling from audit/history.

## 12. Конфигурации и переменные окружения

Values are intentionally not shown.

| Variable / Config Key | Где найдено | Обязательная? | Назначение | Риски / комментарии |
|---|---|---|---|---|
| `DATABASE_URL` | `backend/app/settings.py`, compose | Yes backend | DB connection | Secret-bearing URL, must be masked |
| `POSTGRES_DB` | compose/env example | Yes compose | DB name | Production env only |
| `POSTGRES_USER` | compose/env example | Yes compose | DB user | Sensitive operational config |
| `POSTGRES_PASSWORD` | compose/env example | Yes compose | DB password | Secret |
| `TAKSKLAD_ENV` | settings/compose | Recommended | local/prod behavior | Controls cookie secure default |
| `TAKSKLAD_SERVICE_NAME` | settings/compose | No | Health/readiness identity | Low risk |
| `TAKSKLAD_API_TOKEN` | settings/compose | Yes for protected service-token mode | Bearer auth | High impact if leaked |
| `TAKSKLAD_CORS_ORIGINS` | settings/compose | Optional | CORS allowlist | Credentials disabled currently |
| `TAKSKLAD_TIMEZONE` | settings/compose | Optional | Report/business timezone | Defaults Asia/Tashkent |
| `TAKSKLAD_WEB_LOGIN` | settings/compose | Yes for web auth | Web login | No value in docs |
| `TAKSKLAD_WEB_PASSWORD_HASH` | settings/compose | Yes for web auth | Password verifier | Secret-ish hash |
| `TAKSKLAD_WEB_SESSION_SECRET` | settings/compose | Yes for cookie security | Session signing | Falls back to API token if empty |
| `TAKSKLAD_WEB_SESSION_TTL_SECONDS` | settings/compose | Optional | Session TTL | Defaults 86400 |
| `TAKSKLAD_WEB_COOKIE_SECURE` | settings/compose | Prod yes | Secure cookie flag | Defaults true outside local |
| `TAKSKLAD_WEB_LOGIN_MAX_ATTEMPTS` | settings/compose | Optional | Login lockout | Only login rate limiting |
| `TAKSKLAD_WEB_LOGIN_WINDOW_SECONDS` | settings/compose | Optional | Login lock window | Low risk |
| `TAKSKLAD_WEB_LOGIN_LOCK_SECONDS` | settings/compose | Optional | Login lock duration | Low risk |
| `TAKSKLAD_GOOGLE_SPREADSHEET_ID` | compose/exporter | Yes for Google mirror | Target spreadsheet | Operational config |
| `TAKSKLAD_GOOGLE_SHEET_NAME` | compose/exporter | Optional | Sheet tab name | Defaults `data` |
| `TAKSKLAD_GOOGLE_CREDENTIALS_JSON_BASE64` | compose/exporter | Yes for server Google | Google service account | Secret |
| `TAKSKLAD_GOOGLE_API_TIMEOUT_SECONDS` | compose/exporter | Optional | Google timeout | Operational tuning |
| `TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED` | settings/compose | Optional | Google -> backend sync | Off by default, risky if enabled casually |
| `GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS` | compose | Optional | Worker interval | Load/lag tuning |
| `SKLADBOT_API_TOKEN` | compose | Yes for SkladBot | SkladBot token | Secret |
| `SKLADBOT_API_TOKENS` | compose | Optional | Token rotation/pool | Secret |
| `SKLADBOT_API_BASE_URL` | compose | Optional | SkladBot URL | Vendor dependency |
| `SKLADBOT_CREATE_REQUESTS_MODE` | compose | Important | dry_run/live mode | Must stay controlled |
| `SKLADBOT_SKU_MAPPING_JSON` | compose | Optional | SKU mapping override | Invalid config can block dry-run |
| `SMARTUP_AUTO_IMPORT_ENABLED` | compose | Optional | Enable worker | Default false |
| `SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED` | compose | Optional | Enable backend import | Default false |
| `SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED` | compose | Optional | Enable status write-back | Default false |
| `SMARTUP_BASE_URL` | compose | Yes if Smartup enabled | Smartup API base | External dependency |
| `SMARTUP_USERNAME` | compose | Yes if Smartup enabled | Smartup login | Secret |
| `SMARTUP_PASSWORD` | compose | Yes if Smartup enabled | Smartup password | Secret |
| `SMARTUP_PROJECT_CODE` | compose | Yes if Smartup enabled | Smartup project | Sensitive operational config |
| `SMARTUP_FILIAL_ID` | compose | Yes if Smartup enabled | Smartup filial | Sensitive operational config |
| `TELEGRAM_BOT_TOKEN` | compose | Yes if Telegram enabled | Bot API token | Secret |
| `TELEGRAM_ALLOWED_CHAT_IDS` | compose | Recommended | Chat allowlist | Should be set in production |
| `TELEGRAM_ADMIN_CHAT_IDS` | compose | Recommended | Admin command allowlist | Empty means fail-open admin behavior |
| `TELEGRAM_WORKER_MAX_FILE_BYTES` | compose | Optional | Telegram file limit | Good protection, default 20 MB |
| `TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS` | compose | Optional | Report recipients | Sensitive IDs |
| `YANDEX_GEOCODER_API_KEY` | compose | Optional | Geocoding | Secret/API quota |
| `TAKSKLAD_BACKEND_HOST` | compose | Yes deploy | Backend public host | Routing |
| `TAKSKLAD_FRONTEND_HOST` | compose | Yes deploy | Frontend public host | Routing |
| `TRAEFIK_NETWORK` | compose | Optional | External Docker network | Must match Traefik stack |

Local desktop config files include credentials/runtime JSON and Telegram settings. They are gitignored and were not read.

## 13. Зависимости и внешние сервисы

| Dependency / Service | Тип | Где используется | Назначение | Риски / комментарии |
|---|---|---|---|---|
| FastAPI/Uvicorn | Backend runtime | `backend/app/main.py` | HTTP API | Needs auth/rate hardening |
| SQLAlchemy/Alembic/psycopg | DB layer | backend app/migrations | ORM/migrations/Postgres driver | Query patterns need optimization |
| Pydantic | Validation | `schemas.py` | DTO validation | Flexible import DTO needs stricter model |
| openpyxl/pandas | File processing | import/report modules | Excel import/export | Heavy but expected for warehouse workflow |
| gspread/oauth2client | Google Sheets | desktop/backend exporters | Mirror/fallback | Quotas/rate limits |
| Pillow | Assets/images | desktop/product image tests | Image handling | Low risk |
| PyInstaller | Packaging | release workflow | Windows desktop build | Build reproducibility important |
| React/React DOM | Frontend | `frontend` | Web admin UI | No frontend tests |
| TypeScript/Vite | Frontend build | `frontend/package.json` | Typecheck/build | Build passes |
| lucide-react | UI icons | frontend | Icons | Low risk |
| Nginx | Frontend runtime | frontend Docker | Static app and API proxy | Security headers present |
| Traefik | Edge proxy | deploy labels | HTTPS routing | External network must match |
| PostgreSQL | Database | compose | Source of truth | Backup/migration discipline required |
| Telegram Bot API | External API | telegram worker/desktop legacy | Excel ingest/report commands | Chat allowlists and tokens critical |
| SkladBot API | External WMS | SkladBot modules | Requests, matching, returns | Rate limits, token rotation, matching risk |
| Smartup API | External source | Smartup worker | Auto import/status change | Write-back flags must stay controlled |
| Yandex Geocoder | External API | geocoding/logistics | Coordinates | API quota/secret |
| GitHub Actions | CI/CD | workflows | Checks/deploy/release | Current status not checked |
| Docker Compose | Runtime | deploy/vds | Multi-service production | Requires env and network correctness |

Potentially unused dependencies were not proven. A dependency graph/audit tool pass would be needed before removing anything.

Security-sensitive dependencies/services:

- Google service account handling.
- Telegram bot token and chat IDs.
- SkladBot token pool.
- Smartup credentials.
- Postgres credentials.
- Web session secret/API token.

## 14. Authentication, authorization и безопасность

| Severity | Issue | Evidence | Impact | Recommendation |
|---|---|---|---|---|
| High | No explicit CSRF token or Origin/Referer check for cookie-auth unsafe methods | Session cookie is `SameSite=Lax`; frontend sends `credentials: "include"`; state-changing routes use cookie/session permissions | Browser session could be abused in same-site/subdomain scenarios | Add CSRF token or strict Origin/Referer validation for POST/unsafe methods |
| High | Import API accepts arbitrary row dict list without explicit size/field limits | `ImportCreate.rows: list[dict[str, Any]]`; service processes `len(payload.rows)` and normalizes each raw row | Memory/DB pressure and raw payload sprawl | Add request body/row/field limits, strict row schema, raw payload trimming/redaction |
| Medium | Service token maps to full admin role | `read_auth_context()` returns `ROLE_ADMIN` for valid Bearer token | One leaked token grants all write actions | Split token scopes: desktop scan, worker, admin; add rotation and audit |
| Medium | No general API-wide rate limit | Login lockout exists, but no global per-IP/user/token limiter found | Bad client or leaked token can overload DB/workers | Add middleware or proxy rate limits for expensive/write endpoints |
| Medium | Telegram admin allowlist fail-open | `is_admin_chat()` returns true when `admin_chat_ids` is empty | In production misconfig, any allowed chat can become admin | Fail-closed for production or add startup validation |
| Medium | Personal representative phone can be included in SkladBot comment | `build_representative_comment()` appends `personal_phone` | Personal phone exposure to external WMS/comments | Make personal phone opt-in, mask by default, document lawful basis |
| Medium | No dependency/security audit gate in CI | CI runs tests/build/compose, not `pip-audit` or `npm audit` | Known vulnerable deps may pass CI | Add scheduled and PR dependency audit gate |
| Low | CSP allows `style-src 'unsafe-inline'` | Traefik label and Nginx template | Weaker XSS defense-in-depth | Remove inline styles or document controlled exception |
| Low | Legacy desktop Telegram file guard is not whitelist-first | Desktop Telegram service path exists | Unexpected local file send if desktop polling is enabled | Restrict safe dirs/extensions |
| Info | Secret hygiene exists | `.gitignore`, release preflight tracked secret scan, redaction module | Positive control | Keep and expand to docs/artifacts/support bundles |
| Info | Public Postgres port not exposed in compose | `postgres` only on internal network | Positive infra control | Keep Adminer profile disabled by default |
| Info | Login has lockout | `ensure_login_not_locked()` and settings | Positive auth control | Extend rate limiting beyond login |

Checked security areas:

- Auth methods: Bearer token, web session cookie, local-dev fallback.
- Authorization: role permissions for admin/client-points writes.
- Password handling: password hash based web auth, no plain password storage found in code.
- CORS: configurable allowlist, credentials false.
- Secrets: local sensitive files present but gitignored; contents not read.
- File uploads/imports: Telegram file max size exists; backend import body limits need hardening.
- Output redaction: redaction module exists for secrets/KIZ-like strings.

Unknown:

- Real production env values and whether allowlists/secrets are correctly configured.
- Current dependency CVEs.
- Current GitHub secret configuration.
- Live WAF/proxy-level rate limits, if any.

## 15. Обработка ошибок, логирование и observability

Existing observability:

- `/health`: basic liveness and version/environment.
- `/ready`: DB, migration, queues, Google mirror/import readiness.
- `/api/v1/readiness`: authenticated readiness variant.
- `pending_events`: integration queue state, attempts, last errors.
- `incidents`: operational issue tracking.
- `audit_log`: business/admin action trail.
- `operations_service`: hot-path vs mirror/integration attention.
- Docker healthchecks for API/frontend/workers/Postgres.
- Deploy log scan for `ERROR|CRITICAL|Traceback|Exception|panic`.
- Desktop startup checks, diagnostics and last-good storage backup behavior.

Strengths:

- Queue errors are not purely lost in logs; they can be represented as event status/incidents.
- Readiness checks include migration head awareness.
- Release/deploy tooling scans tracked secrets and fresh logs.
- Desktop tests cover failure messaging, fallback and corrupted local state recovery.

Weaknesses:

- No external metrics/telemetry stack found.
- No SLOs or alert routing policy found.
- Some UI context refresh errors can be swallowed.
- Logs can be noisy during expected negative-path tests.
- Production live state was not checked in this audit.

Recommended observability additions:

1. Add metrics for queue lag, failed events, scan write latency, Google export lag, SkladBot/Smartup error counts.
2. Add alert thresholds for hot-path queues and readiness degraded status.
3. Keep a short operator runbook mapping readiness failures to exact commands.
4. Ensure support bundles always use redacted startup/self-check data.

## 16. Тесты и quality controls

| Область качества | Инструмент / доказательство | Текущее состояние | Рекомендация |
|---|---|---|---|
| Python tests | `unittest discover -s tests` | Local OK, `777` tests | Keep as mandatory gate |
| Test files | `tests/test*.py` | 59 files | Good breadth |
| Python compile | `compileall` | Local OK | Keep in CI |
| DB migration head | Alembic heads | Local OK, `20260701_0007` | Add migration application test on temp DB if not already enough |
| Frontend build/typecheck | `npm --prefix frontend run build` | Local OK | Keep in CI |
| Frontend tests | package scripts | No test script found | Add component/API client tests |
| E2E/browser tests | none found | Not covered | Add Playwright smoke for login/admin tabs if feasible |
| Shell syntax | `bash -n deploy/vds/*.sh` | Local OK | Keep |
| Docker config | compose config | Local OK | Keep |
| Release preflight | `tools/release_preflight.py --skip-network` | Local OK | Keep; run with network before actual release |
| Go/no-go | `tools/release_go_no_go.py` | Exists | Requires acceptance output, not read here |
| Git hooks | `.githooks/pre-commit`, `pre-push` | Main branch discipline | Keep |
| Lint/format | ruff/black/eslint/prettier config not found | Gap | Add minimal lint/format gates |
| Type checking Python | mypy/pyright not found | Gap | Consider mypy/pyright for backend schemas/services |
| Dependency audit | none in CI | Gap | Add `pip-audit`/`npm audit` scheduled and pre-release |

Current git state:

- Branch: `main`.
- Worktree: dirty before and after audit.
- Existing modified files included docs, many `src/taksklad/*`, tests and a PowerShell tool.
- Existing untracked areas included a desktop reliability runbook, `single_instance.py` and a real-data reconciliation folder.
- This audit created only this new Markdown file.

## 17. Build, deployment и infrastructure

| Область | Файл / доказательство | Объяснение | Риски / комментарии |
|---|---|---|---|
| Backend image | `backend/Dockerfile` | Python 3.12 slim, backend requirements, Uvicorn | Build depends on backend deps only |
| Frontend image | `frontend/Dockerfile` | Node build then Nginx static runtime | Build passes locally |
| Production compose | `deploy/vds/docker-compose.yml` | postgres, backend-api, frontend, workers, adminer profile | Real env required, not read |
| Traefik routing | compose labels and `deploy/traefik` | HTTPS host routing and security headers | Host/env correctness critical |
| Nginx auth proxy | `frontend/nginx.conf.template` | Static SPA, auth endpoints, `auth_request` for `/api/` | No CSRF token layer |
| Postgres | compose | Internal network, volume, init SQL | Backup and migrations important |
| Workers | compose | SkladBot, Smartup, Google, Telegram | Share DB and env |
| CI | `.github/workflows/ci.yml` | Python and frontend gates | No dependency audit/lint |
| Production deploy | `.github/workflows/deploy-production.yml` | Manual workflow, validates required secrets, runs local preflight | Current Actions status not checked |
| Remote deploy script | `deploy/vds/deploy_from_git.sh` | Restore point, checkout/sync ref, compose deploy, public checks, log scan | Acceptance optional by mode |
| Rollback docs | `docs/deploy-rollback-runbook.md` | Backup/restore/deploy rollback process | Use for production changes |
| Migrations docs | `docs/database-migrations-runbook.md` | Baseline/upgrade/rollback posture | Downgrade not automatic |
| Windows release | `.github/workflows/build-windows-release.yml` | PyInstaller assets and smoke | Needs real Windows acceptance before rollout |

Production deployment target inferred from docs/scripts:

- app dir default: `/opt/stacks/taksklad/app`;
- public backend health default: `https://api.taksklad.uz/health`;
- Docker services: backend, frontend, Telegram, Google sync, SkladBot, Smartup auto import, Postgres.

This audit did not SSH to VDS and did not check live Docker state.

## 18. Бизнес-логика и domain model

Plain-language domain:

- Заказ - отгрузка клиенту на дату, с типом оплаты, адресом и представителем.
- Позиция заказа - товар и плановое количество коробов/штук.
- КИЗ - маркировочный код, который нельзя повторно отгружать, пока он не возвращен/отменен/сброшен.
- Скан - факт привязки КИЗа к позиции заказа.
- Возврат - событие, после которого КИЗ снова может быть использован.
- Google Sheets - зеркало/витрина и legacy fallback.
- SkladBot - внешняя WMS/заявки/остатки.
- Smartup - внешний источник новых терминальных заказов.
- Telegram - канал загрузки Excel, команд и отчетов.
- Web panel - контроль, ручные действия, инциденты, очереди.

Core rules:

1. DB/Postgres is source of truth.
2. Active orders exclude completed/returned/archived/cancelled/removed states.
3. Required-KIZ items cannot be completed unless planned blocks are scanned, except explicit admin complete-without-KIZ path.
4. Duplicate KIZ on another active item is blocked unless latest movement makes it available.
5. Return writes KIZ movement so code can be reused.
6. Aggregate box scans must match product and not exceed remaining blocks.
7. Google mirror can lag behind DB.
8. SkladBot request creation can run in dry-run or live mode, controlled by env.
9. Smartup auto import write-back flags are off unless enabled by env.
10. Manual admin actions should write audit context and reasons.

User roles:

- `admin`: full write permissions.
- `logistics_slots`: client-point/logistics oriented permissions.
- `operator`: limited role in web auth model.
- `service-token`: currently maps to admin.

Hidden assumptions and edge cases:

- Address matching in SkladBot diagnostics is useful but not always a hard blocking condition.
- Partial returns appear limited; return UX can imply item selection, but backend/product logic should be checked before promising partial return support.
- Smartup import recovery depends on audit/history because external status change happens after backend import.
- Google->backend sync is disabled by default and should not be enabled casually.
- Local desktop fallback behavior must be treated as operational backup, not source of truth replacement.

## 19. Важные code paths

### Code Path: Excel import into backend

- Назначение: Convert Excel/normalized rows into backend orders/items.
- Trigger: Telegram document, desktop import, web/admin import endpoint.
- Участвующие файлы: `backend/app/excel_importer.py`, `backend/app/imports_service.py`, `backend/app/main.py`, `backend/app/google_sheets_pending.py`, `backend/app/skladbot_request_dry_run.py`.
- Flow:
  1. Parse Excel into row payload.
  2. Build `ImportCreate`.
  3. `create_import()` creates `ImportJob`.
  4. Load existing order/item keys.
  5. Normalize each row.
  6. Skip invalid/duplicate rows.
  7. Create/update orders and items.
  8. Queue Google mirror.
  9. Create SkladBot dry-run.
- Reads/writes: DB orders/items/imports/import_files/pending_events/audit.
- External calls: Google/SkladBot via queued or dry-run paths.
- Failure modes: invalid rows, duplicate file/rows, Google queue failure, dry-run failure, oversized flexible payload.
- Комментарии: central ingest path; needs payload limits.

### Code Path: Desktop active order refresh

- Назначение: Load current orders for warehouse desktop.
- Trigger: app startup, refresh button/timer.
- Участвующие файлы: `src/taksklad/app_data_loading.py`, `desktop_refresh_service.py`, `backend_client.py`, Google fallback modules.
- Flow:
  1. Desktop starts refresh task.
  2. Try backend active orders/readiness depending flags.
  3. Optionally fallback to Google Sheets.
  4. Reconcile current selected order.
  5. Update UI state and warnings.
- Reads/writes: backend orders, local app state, local diagnostics.
- External calls: backend API, Google Sheets fallback.
- Failure modes: backend unavailable, fallback disabled, SkladBot unavailable, stale UI.
- Комментарии: tests cover fallback/error paths.

### Code Path: KIZ scan

- Назначение: Safely bind scanned KIZ to order item.
- Trigger: scanner input in desktop or API scan request.
- Участвующие файлы: `src/taksklad/app_scanning.py`, `src/taksklad/desktop_scan_rules.py`, `src/taksklad/backend_events.py`, `backend/app/orders_service.py`, `backend/app/kiz_movements_service.py`.
- Flow:
  1. Desktop validates format/local rules.
  2. Backend checks availability.
  3. Lock order item.
  4. Lock KIZ advisory transaction.
  5. Check duplicate/movement/product/remaining blocks.
  6. Insert scan and movement.
  7. Update item status.
  8. Queue Google update.
- Reads/writes: `order_items`, `scan_codes`, `kiz_codes`, `kiz_movements`, audit, pending events.
- External calls: Google via queue.
- Failure modes: duplicate, wrong SKU, full item, backend down, Google lag.
- Комментарии: strongest domain safety path.

### Code Path: Undo scan

- Назначение: Remove/reverse latest scan.
- Trigger: desktop/admin undo.
- Участвующие файлы: `backend/app/orders_service.py`, desktop scanning modules.
- Flow:
  1. Validate scan/order item.
  2. Lock KIZ.
  3. Record `undo` movement.
  4. Delete scan or recompute state.
  5. Queue Google update.
- Reads/writes: scan, item status, movement, audit.
- Failure modes: scan missing, stale local state, Google lag.
- Комментарии: undo is key to safe warehouse corrections.

### Code Path: Complete order

- Назначение: Mark order complete after required items scanned.
- Trigger: desktop finish/admin action.
- Участвующие файлы: `src/taksklad/app_finish.py`, `backend/app/orders_service.py`, `backend/app/order_actions_service.py`.
- Flow:
  1. Check required items.
  2. Reject if incomplete.
  3. Set item/order completed.
  4. Write audit.
  5. Queue Google archive/export.
- Reads/writes: order, items, audit, pending events.
- Failure modes: incomplete items, backend down, mirror lag.
- Комментарии: complete-without-KIZ exists as admin action and must stay audited.

### Code Path: Return order/KIZ reuse

- Назначение: Mark completed order returned and free KIZs for future outbound.
- Trigger: return lookup/confirmation.
- Участвующие файлы: `src/taksklad/app_returns.py`, `backend/app/orders_service.py`, `backend/app/skladbot_return_requests.py`.
- Flow:
  1. Lookup completed order.
  2. Confirm return.
  3. Set order returned.
  4. Record `return` movement for KIZ scans.
  5. Queue SkladBot return request.
  6. Queue Google return/archive export.
- Reads/writes: order, movements, pending events, audit.
- External calls: SkladBot/Google via queues.
- Failure modes: order not completed, partial return mismatch, queue failure.
- Комментарии: movement ledger is the key invariant.

### Code Path: Google Sheets mirror

- Назначение: Keep spreadsheet representation in sync with DB.
- Trigger: import/scan/undo/complete/return/admin actions.
- Участвующие файлы: `backend/app/google_sheets_pending.py`, `google_sheets_exporter.py`, `google_sheets_sync_worker.py`.
- Flow:
  1. Service queues event with idempotency key.
  2. Worker picks pending/failed event.
  3. Exporter writes to Google.
  4. Event status is updated.
  5. Readiness/operations reflect lag/failure.
- Reads/writes: pending events, Google Sheets.
- Failure modes: rate limit, missing row, credentials, network.
- Комментарии: mirror lag is normal and must be monitored.

### Code Path: SkladBot request matching/create

- Назначение: Match orders to SkladBot requests and optionally create requests.
- Trigger: import dry-run, worker sync, admin resync.
- Участвующие файлы: `backend/app/skladbot_worker.py`, `skladbot_request_dry_run.py`, `skladbot_diagnostic.py`.
- Flow:
  1. Select active/recent orders.
  2. Fetch candidate SkladBot requests.
  3. Match by date/client/payment/products and diagnostics.
  4. Store found/missing/multiple status.
  5. Queue create event if mode allows.
- Reads/writes: orders raw payload, pending events, dry-run payloads.
- External calls: SkladBot API.
- Failure modes: 429/401/timeout, multiple match, wrong SKU mapping, address mismatch.
- Комментарии: keep dry-run mode as default unless explicitly accepted.

### Code Path: Telegram document import

- Назначение: Import Excel through Telegram with shipment date prompt.
- Trigger: Telegram user sends document.
- Участвующие файлы: `backend/app/telegram_worker.py`, `excel_importer.py`, backend import API/service.
- Flow:
  1. Poll Telegram update.
  2. Validate chat/file size.
  3. Ask or read shipment date.
  4. Download file.
  5. Parse/import through backend.
  6. Send result/report.
- Reads/writes: Telegram state events, imports, orders.
- External calls: Telegram API.
- Failure modes: file timeout, invalid date, backend unavailable, admin allowlist misconfig.
- Комментарии: central non-desktop ingest path.

### Code Path: Smartup auto import

- Назначение: Scheduled import from Smartup terminal orders.
- Trigger: worker schedule or manual run.
- Участвующие файлы: `backend/app/smartup_auto_import.py`, `smartup_auto_import_worker.py`, `smartup_auto_import_history_service.py`.
- Flow:
  1. Check schedule/slot/flags.
  2. Fetch Smartup orders.
  3. Build preview/audit.
  4. Create backend imports.
  5. Change Smartup status.
  6. Queue SkladBot for successful deals.
  7. Send exports/reports.
- Reads/writes: DB imports/orders/history/audit, outputs.
- External calls: Smartup, SkladBot, Telegram.
- Failure modes: external status failure after import, slot duplicate, geocoding failure.
- Комментарии: should stay heavily guarded by env flags and audit.

## 20. Анализ документации

| Документ | Текущее содержание | Пробелы | Рекомендация |
|---|---|---|---|
| `README.md` | Product overview, process, dev commands | Must be checked against current code | Keep as quickstart |
| `docs/README.md` | Docs index with ACTIVE/HISTORY/SENSITIVE statuses | Strong doc hygiene | Keep updated after new audit |
| `docs/taksklad-system-stack-overview.md` | Broad architecture/product overview | Mentions version `2.0.24`, code now `2.0.25` | Update version/date |
| `docs/report-source-rules.md` | DB-first/report source rules | Not deeply audited here | Keep as source-of-truth policy |
| `docs/local-development-setup.md` | Local setup/check commands | Currently modified in worktree | Reconcile with latest checks |
| `docs/database-migrations-runbook.md` | Migration lifecycle and rollback posture | No automatic downgrade strategy | Keep explicit backup/forward posture |
| `docs/deploy-rollback-runbook.md` | Production deploy/rollback | Live state not checked | Keep tied to deploy script |
| `docs/event-queue-lifecycle.md` | Queue lifecycle | Not fully revalidated | Good candidate for operations guide |
| `docs/manual-acceptance-runbook.md` | Manual acceptance | Needs current run when release planned | Use before rollout |
| `docs/windows-backend-acceptance.md` | Windows acceptance for backend flags | Not run here | Keep for workstation validation |
| `docs/implementation-log.md` | Agent/deploy evidence history | Can become long/noisy | Keep chronological, summarize periodically |
| `docs/changelog.md` | User/release changes | Needs discipline | Keep product-facing |
| `docs/skladbot-api-key-functionality.md` | Sensitive historical SkladBot API reference | Sensitive, not quoted | Keep marked `SENSITIVE_HISTORY` |
| `docs/taksklad-feature-user-stories.xlsx` | Canonical feature/user stories workbook | Binary, not read here | Use for product acceptance, not source code audit |
| `docs/taksklad-full-functionality.md` | Older full functionality doc | Version `1.1.17`, stale architecture | Do not use as current source without cross-check |
| `docs/project-architecture.md` | Older architecture reference | Marked UPDATE/HISTORY | Fold useful pieces into current architecture docs |

Recommended docs cleanup:

1. Update `taksklad-system-stack-overview.md` to `2.0.25`.
2. Add this audit to `docs/README.md` as HISTORY or ACTIVE audit reference.
3. Split a stable `architecture.md` from historical implementation logs.
4. Add dedicated `security-hardening.md`.
5. Add `api-contracts.md` generated or manually synchronized from schemas/routes.

## 21. Maintainability и качество кода

Severity labels here are maintainability severity, not security severity.

| Severity | Finding | Evidence | Impact | Recommendation |
|---|---|---|---|---|
| High | Web app is monolithic | `frontend/src/App.tsx` 3462 lines | Hard to change safely | Split into feature panels/components/hooks |
| High | Large workers/services | `telegram_worker.py` 2611, `smartup_auto_import.py` 2174, `skladbot_request_dry_run.py` 1465 | High cognitive load and regression risk | Extract small services and pure functions by workflow |
| Medium | API client is large | `frontend/src/api.ts` 800 lines | DTO/action coupling grows | Split by domain: auth, orders, incidents, reports, clients |
| Medium | CSS is large single file | `styles.css` 2202 lines | Hard to reason about UI regressions | Split by panel or introduce CSS modules/conventions |
| Medium | Full-load Python filtering in backend | admin/reports/import dedup paths | Scaling risk | Move filters/pagination to SQL |
| Medium | Mixed historical/current docs | docs index marks stale docs | New agents can pick wrong truth | Keep docs index strict and update current overview |
| Low | No formal formatter/linter config found | no ruff/eslint/prettier config found | Style drift | Add minimal lint rules |
| Low | No frontend tests | package scripts only build/dev/preview | UI logic regressions may pass | Add focused tests for API and critical panels |

Positive maintainability signs:

- Clear domain services exist in backend.
- Tests cover many business and failure paths.
- Docs explicitly mark ACTIVE/HISTORY/SENSITIVE.
- Release/deploy tooling is scripted.
- Project instructions strongly protect secrets and main branch discipline.

## 22. Performance и scalability

Confirmed performance risks:

| Area | Evidence | Risk | Recommendation |
|---|---|---|---|
| Admin table | `build_admin_table()` selects all orders/items/scans, then filters and slices in Python | Memory/latency grows with order count | Push filters, counts and pagination into SQL |
| Import dedup | `load_existing_import_keys()` loads all non-returned orders/items | Large import slows as DB grows | Use indexed lookup by source ids/item keys for current import only |
| Day reports | `build_day_report()` and dashboard summary load orders/items/scans and filter in Python | Report latency grows with history | Add date filters in SQL and aggregate queries |
| Representative contact lookup | Loads all active contacts and matches in Python | OK for small list, grows linearly | Add normalized alias table or indexed search if list grows |
| Frontend bundle | 289 KB JS raw, 84 KB gzip | Acceptable now, but monolithic | Split routes/panels if app grows |
| Workers | polling intervals and external API retries | API quotas and worker load | Track queue lag and external 429/timeout metrics |
| Rate limiting | only login lockout confirmed | Backend endpoints can be stressed | Add proxy/app limits |

Concurrency:

- KIZ scan path uses row lock and advisory lock, which is strong for duplicate prevention.
- Pending event idempotency keys help duplicate queue prevention.
- Some scheduled worker slots use audit/locking patterns, but external side effects still require recovery discipline.

What cannot be fully assessed locally:

- Production DB size.
- Real queue lag.
- Live Google/SkladBot/Smartup latency.
- Container CPU/RAM.
- Browser performance on target operator machines.

## 23. Риски, неизвестные зоны и технический долг

| Priority | Category | Issue / Unknown | Evidence | Impact | Recommended Action |
|---|---|---|---|---|---|
| P1 | Security | No CSRF token/origin check for cookie-auth writes | Cookie auth + credentials include + POST admin actions | Unauthorized browser-triggered writes in some scenarios | Add CSRF or Origin/Referer guard |
| P1 | Security/DoS | Import body accepts arbitrary row dict list without limits | `ImportCreate.rows` | Memory/DB pressure | Add limits and strict row schema |
| P1 | Auth | Service token is full admin | `read_auth_context()` | Token leak impact high | Token scopes and rotation |
| P1 | Operations | Live production state not verified in this audit | No SSH/live checks run | Cannot claim production healthy | Run live health/readiness/container checks before release |
| P1 | Data consistency | Smartup import can write backend before external status change succeeds | Smartup flow code | Partial external/internal mismatch | Improve transaction boundary/recovery UI |
| P1 | Scaling | Admin/report/import paths full-load DB rows | service code | Slow web/admin as data grows | SQL filters/pagination/aggregates |
| P2 | Privacy | Representative personal phone can be sent in comment | `representative_contacts.py` | Personal data exposure | Mask or make opt-in |
| P2 | Security | Telegram admin allowlist fail-open when empty | worker code | Misconfig risk | Fail-closed in prod |
| P2 | Maintainability | `App.tsx` and worker files too large | line counts | Higher change risk | Split modules |
| P2 | Quality | No frontend unit/e2e tests found | package scripts | UI regressions can pass build | Add focused tests |
| P2 | Security | No dependency audit gate | CI | CVEs can pass | Add scheduled audit |
| P2 | Docs | Current overview doc version stale | docs says 2.0.24, code 2.0.25 | Agents/devs can trust stale version | Update docs |
| P2 | UX | Some web panel refresh errors can be hidden | frontend context refresh behavior | Stale side panels | Surface warning badges |
| P3 | Frontend routing | Tabs only in local state | no router | No deep links/bookmarks | Add query/hash route state if needed |
| P3 | Styling | Native selects/custom controls inconsistent | `SelectFilter` | UI consistency | Standardize controls |

No P0 blocker was confirmed by local checks. That does not mean production is healthy; it means local source/test/build audit did not expose a confirmed immediate blocker.

## 24. Рекомендованные улучшения

### Срочные исправления

| Что изменить | Почему важно | Часть кодовой базы | Польза | Риск/сложность |
|---|---|---|---|---|
| Add CSRF token or Origin/Referer check for unsafe methods | Protect cookie-auth admin writes | backend/Nginx/frontend | Stronger web security | Medium |
| Add import limits and strict row schema | Prevent oversized/garbage imports | `schemas.py`, `imports_service.py`, Telegram/web import | Better reliability/security | Medium |
| Split service token scopes | Reduce blast radius | auth/settings/deploy env | Safer token leakage posture | Medium |
| Make Telegram admin allowlist fail-closed in production | Avoid misconfig admin exposure | `telegram_worker.py`, settings | Safer ops | Low |
| Mask or opt-in personal representative phone in comments | Reduce personal data exposure | `representative_contacts.py`, SkladBot comment flow | Privacy control | Low |

### Краткосрочные улучшения

| Что изменить | Почему важно | Часть кодовой базы | Польза | Риск/сложность |
|---|---|---|---|---|
| Move admin table filters/pagination to SQL | Current Python slicing will not scale | `admin_service.py` | Faster web panel | Medium |
| Move day reports to SQL date filtering/aggregates | Current reports full-load orders | `reports_service.py` | Faster reports | Medium |
| Optimize import dedup lookups | Current dedup full-loads existing orders | `imports_service.py` | Faster large imports | Medium |
| Add dependency audit CI job | Catch known CVEs | `.github/workflows/ci.yml` | Security gate | Low |
| Add frontend tests for auth/API/admin actions | Build alone is not enough | `frontend` | Fewer UI regressions | Medium |
| Update docs version to 2.0.25 | Avoid stale source-of-truth | `docs/taksklad-system-stack-overview.md` | Better onboarding | Low |

### Среднесрочные улучшения

| Что изменить | Почему важно | Часть кодовой базы | Польза | Риск/сложность |
|---|---|---|---|---|
| Split `App.tsx` into panels/hooks | Current file is too large | frontend | Safer UI changes | Medium |
| Split `telegram_worker.py` by command/import/report concerns | Current file is too large | backend worker | Easier fixes | Medium |
| Split Smartup workflow into state machine steps | External/internal partial states are hard | Smartup modules | Better recovery | High |
| Add Playwright smoke for web admin | No e2e coverage | frontend/CI | Catches real browser regressions | Medium |
| Add metrics endpoint/exporter | Readiness is not enough for trend monitoring | backend/workers | Better ops | Medium |
| Add runbook for queue lag and degraded readiness | Operators need exact actions | docs/operations | Faster incident response | Low |

### Долгосрочные улучшения

| Что изменить | Почему важно | Часть кодовой базы | Польза | Риск/сложность |
|---|---|---|---|---|
| Introduce scoped internal clients for desktop/workers/admin | Different actors need different powers | auth/API/deploy | Strong security boundary | High |
| Normalize external integration state machines | SkladBot/Smartup/Google have different failure semantics | backend services | More reliable recovery | High |
| Add archival/partitioning strategy for orders/scans/events | Data will grow | DB/migrations/services | Predictable scale | High |
| Build a stable API contract doc from schemas/routes | Future agents/devs need synchronized contracts | docs/tooling | Less drift | Medium |
| Add production dashboard for queue/error/SLA | Manual checks do not scale | infra/backend | Better operations | High |

## 25. Будущая документация

### README

Suggested structure:

1. What TakSklad does.
2. Runtime components: desktop, backend, frontend, workers, DB.
3. Source of truth: Postgres vs Google mirror.
4. Quick local setup.
5. Common verification commands.
6. Where to find runbooks.
7. What not to read/commit: secrets, outputs, real reports.

### Setup guide

1. macOS/Python/Node prerequisites.
2. Python venv install.
3. Frontend install.
4. Docker local backend/Postgres.
5. Example env only.
6. Running desktop.
7. Running backend.
8. Running tests.
9. Troubleshooting.

### Architecture guide

1. High-level diagram.
2. DB-first data model.
3. Desktop/backend/frontend responsibilities.
4. Worker responsibilities.
5. KIZ movement ledger.
6. Import/scan/return flows.
7. Queue/event lifecycle.
8. External integrations.
9. Known invariants.

### API documentation

1. Auth/session.
2. Orders and scans.
3. Returns.
4. Imports.
5. Admin actions.
6. Incidents/events/operations.
7. Reports.
8. Logistics/client points.
9. Error format and common status codes.

### Environment variable guide

1. Backend core.
2. Web auth.
3. Google Sheets.
4. SkladBot.
5. Telegram.
6. Smartup.
7. Deploy/Traefik.
8. Secrets rotation.
9. Safe local examples.

### Deployment guide

1. Git branch and clean source rules.
2. CI gate.
3. Production workflow inputs.
4. Remote deploy script behavior.
5. Migrations.
6. Health/readiness.
7. Log scan.
8. Rollback.
9. Post-deploy acceptance.

### Testing guide

1. Fast local checks.
2. Full unittest suite.
3. Frontend build.
4. Docker compose config.
5. Release preflight.
6. Windows acceptance.
7. Manual warehouse acceptance.
8. Live smoke checks.

### Contributor guide

1. Main branch policy.
2. How to avoid secrets.
3. Test expectations.
4. Docs update expectations.
5. Code style.
6. Review checklist.
7. Warehouse safety invariants.

## 26. Индекс важных файлов

| File | Purpose | Importance | Notes |
|---|---|---|---|
| `AGENTS.md` | Project-specific agent rules | Critical | Read before work |
| `README.md` | Product/dev entrypoint | Critical | Main human quickstart |
| `main.py` | Desktop dev entrypoint | Critical | Runs app/smokes |
| `pyinstaller_entry.py` | Packaged desktop entrypoint | High | Windows build path |
| `src/taksklad/main.py` | Desktop app composition | Critical | `ScanningApp` |
| `src/taksklad/app_scanning.py` | Desktop scan flow | Critical | Warehouse hot path |
| `src/taksklad/desktop_scan_rules.py` | Desktop duplicate/SKU rules | Critical | Operator safety |
| `src/taksklad/backend_client.py` | Desktop backend API client | Critical | Backend bridge |
| `src/taksklad/backend_events.py` | Desktop pending backend queue | Critical | Offline/retry behavior |
| `src/taksklad/app_returns.py` | Desktop returns flow | High | KIZ reuse workflow |
| `src/taksklad/storage.py` | Local storage/backups | High | Reliability |
| `src/taksklad/update_service.py` | Desktop update checks | High | Windows rollout |
| `src/taksklad/startup_check.py` | Startup diagnostics | High | Support/readiness |
| `backend/app/main.py` | FastAPI routes/auth | Critical | Backend entry |
| `backend/app/models.py` | ORM models | Critical | DB domain |
| `backend/app/schemas.py` | API DTOs | Critical | Contract source |
| `backend/app/settings.py` | Backend config | Critical | Env contract |
| `backend/app/db.py` | DB session | Critical | SQLAlchemy entry |
| `backend/app/orders_service.py` | Order/scan/return logic | Critical | KIZ integrity |
| `backend/app/kiz_movements_service.py` | KIZ ledger/locks | Critical | Duplicate/reuse safety |
| `backend/app/imports_service.py` | Import creation | Critical | Ingest path |
| `backend/app/admin_service.py` | Admin table | High | Web operations, performance risk |
| `backend/app/order_actions_service.py` | Manual admin actions | High | Audit/action safety |
| `backend/app/google_sheets_pending.py` | Google event queue | High | Mirror consistency |
| `backend/app/google_sheets_exporter.py` | Google export implementation | High | External mirror |
| `backend/app/google_sheets_sync_worker.py` | Google worker | High | Async processing |
| `backend/app/skladbot_worker.py` | SkladBot sync | High | External WMS |
| `backend/app/skladbot_request_dry_run.py` | SkladBot dry-run/create | High | Request safety |
| `backend/app/skladbot_return_requests.py` | SkladBot returns | High | Return workflow |
| `backend/app/telegram_worker.py` | Telegram import/commands | High | External ingest |
| `backend/app/smartup_auto_import.py` | Smartup import workflow | High | External source/write-back |
| `backend/app/health_service.py` | Readiness | High | Ops |
| `backend/app/operations_service.py` | Operations attention | High | Admin visibility |
| `backend/app/event_queue_service.py` | Queue diagnostics/retry | High | Ops |
| `backend/app/incidents_service.py` | Incidents | High | Ops |
| `backend/app/redaction.py` | Secret/KIZ redaction | High | Support/security |
| `backend/migrations/versions/*` | DB migrations | Critical | Current head `20260701_0007` |
| `backend/sql/001_initial_schema.sql` | Bootstrap schema | High | New DB init |
| `frontend/src/App.tsx` | Web admin SPA | High | Large hotspot |
| `frontend/src/api.ts` | Web API client/types | High | Contract mirror |
| `frontend/src/styles.css` | Web styling | Medium | Large CSS file |
| `frontend/nginx.conf.template` | Frontend proxy/auth/security headers | High | Production web boundary |
| `frontend/package.json` | Frontend scripts/deps | High | Build source |
| `deploy/vds/docker-compose.yml` | Production services | Critical | Runtime composition |
| `deploy/vds/deploy_from_git.sh` | Production deploy script | Critical | Backup/sync/health/log scan |
| `deploy/vds/.env.example` | Env example | High | Safe config reference |
| `deploy/traefik/docker-compose.yml` | Traefik stack | High | HTTPS routing |
| `.github/workflows/ci.yml` | CI gate | Critical | Tests/build |
| `.github/workflows/deploy-production.yml` | Production deploy workflow | Critical | Manual deploy |
| `.github/workflows/build-windows-release.yml` | Windows release workflow | High | Desktop artifacts |
| `.githooks/pre-commit` | Branch discipline | High | Blocks non-main commits |
| `.githooks/pre-push` | Branch discipline | High | Blocks non-main push |
| `tools/release_preflight.py` | Release checks | Critical | Local preflight |
| `tools/release_go_no_go.py` | Acceptance verdict | Critical | Release decision |
| `tools/build_windows_test_archive.ps1` | Windows build helper | High | Windows package |
| `docs/README.md` | Docs index/status | High | Prevents stale doc misuse |
| `docs/deploy-rollback-runbook.md` | Deploy/rollback | High | Production ops |
| `docs/database-migrations-runbook.md` | Migration runbook | High | DB ops |
| `docs/local-development-setup.md` | Local setup | High | Developer onboarding |
| `docs/taksklad-system-stack-overview.md` | Architecture overview | High | Needs version update |
| `version.json` | Release manifest | Critical | Desktop rollout |

## 27. Финальное резюме

TakSklad is a warehouse automation monorepo with a real production architecture:

- Windows desktop app for scan operations.
- FastAPI/PostgreSQL backend as source of truth.
- React web-admin panel.
- Workers for Telegram, Google Sheets, SkladBot and Smartup.
- Docker/Traefik/Nginx production setup.
- CI, release preflight and rollback docs.

Health verdict from local audit:

- Source/test/build state: good.
- Domain safety around KIZ scan/reuse: strong.
- Operational tooling: solid for an internal warehouse system.
- Web/security hardening: needs work.
- Performance/scaling posture: acceptable for current scale, risky as data grows.
- Maintainability: backend/domain is understandable, but frontend and workers need modularization.
- Production live state: not confirmed here.

Top strengths:

1. Postgres source of truth.
2. KIZ movement ledger.
3. Backend scan locks and validation.
4. Async queue model for external integrations.
5. Good Python test coverage.
6. CI includes backend and frontend build gates.
7. Deploy script has restore point and health checks.
8. Docs index separates active/history/sensitive docs.
9. Desktop reliability work exists around startup/storage/update/diagnostics.
10. Release tools exist for preflight and go/no-go.

Top weaknesses:

1. Missing CSRF/origin hardening for web cookie-auth writes.
2. Import endpoint lacks explicit size/schema limits.
3. Service token is full admin.
4. No general API rate limiting.
5. Telegram admin allowlist fail-open if empty.
6. Personal phone can be exported into representative comment.
7. Several backend queries full-load DB rows.
8. Web app is one huge `App.tsx`.
9. No frontend tests/e2e found.
10. Some current docs contain stale version references.

Top-10 next actions:

1. Add CSRF or Origin/Referer check for all unsafe session-auth routes.
2. Add hard limits and strict schema to import payloads.
3. Split service token into scoped tokens.
4. Fail-close Telegram admin mode in production.
5. Mask or opt-in representative personal phone export.
6. Move admin table pagination/filtering to SQL.
7. Move report date filtering and dashboard aggregates to SQL.
8. Split `frontend/src/App.tsx` into panels/hooks and add frontend tests.
9. Add dependency/security audit gate to CI.
10. Update current architecture docs from `2.0.24` to `2.0.25` and link this audit in `docs/README.md`.

Additional information that would improve the audit:

- Current production `/health` and `/ready` responses.
- Current production `docker compose ps`, worker logs and queue counts.
- Current GitHub Actions status for `main`.
- Current DB row counts for orders/items/scans/events/incidents.
- Real operational acceptance results from `tools/release_go_no_go.py`.
- Confirmed production env posture for web auth, Telegram allowlists, SkladBot mode and Smartup flags.
