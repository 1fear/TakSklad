# Full Project Audit Architecture Runbook

Дата аудита: 2026-07-07

Проект: TakSklad

Рабочий каталог: `/Users/anton/Documents/work/TakSklad`

Текущая ветка на момент аудита: `main`

Текущий HEAD на момент аудита: `c7b3ecffda55ff3ae7ff4e3bc8b2edebe5c06866`

Статус аудита: `REPO_CODE_DOCS_TEST_AUDIT_READY__LIVE_NOT_VERIFIED`

## 1. Граница аудита

Этот файл фиксирует read-only аудит проекта TakSklad по локальному checkout.

Разрешенные действия в рамках аудита:

- читать repo docs, код, тесты, workflow, deploy scripts и безопасные локальные артефакты;
- читать sanitized `.supergoal`-артефакты по Daily SkladBot и Chapman;
- запускать безопасные локальные проверки без production side effects;
- создать этот один главный файл runbook-аудита.

Что не выполнялось:

- production deploy;
- restart контейнеров или сервисов;
- миграции;
- запись в БД;
- запись в SkladBot, Smartup, Google Sheets;
- отправка Telegram сообщений или документов;
- запуск `/skladbot_daily`;
- commit, push, PR;
- чтение `.env*`, credentials, `Пароли.md`, `/Users/anton/.codex/LOCAL_SECRETS.md`;
- чтение raw client exports, backups, real reports as source of truth.

## 2. Источники истины

| Слой | Статус | Что подтверждает | Ограничение |
|---|---:|---|---|
| Docs truth | Подтверждено локально | Намерение, runbook, история решений | Часть docs устарела относительно code truth |
| Code truth | Подтверждено локально | Реальная реализация в backend, frontend, desktop, deploy scripts | Не равно live runtime |
| Test truth | Частично подтверждено | Две релевантные unittest suites и compileall прошли | Полный test suite не запускался в этом аудите |
| Local artifact truth | Подтверждено частично | `.supergoal` и Chapman brief дают sanitized историю инцидентов | Не заменяет live DB/API/operator truth |
| Live truth | Не подтверждено | Фактические контейнеры, очереди, `/ready`, Telegram, SkladBot, Smartup | Нужен отдельный read-only runtime smoke |
| Data truth | Не подтверждено | Реальные складские данные, DB rows, выгрузки | Raw data intentionally not read |
| Operator truth | Не подтверждено | Физический склад, фактическое получение отчетов | Нужна проверка оператором |

Короткий вывод: архитектура и ключевые риски подтверждены по repo/code/docs/tests. Фактическое production-состояние не проверялось.

## 3. Executive Summary

TakSklad - рабочая складская система вокруг Excel-заказов, КИЗов, Postgres backend, desktop app, web admin, Telegram, SkladBot, Smartup, Google Sheets, XLSX reports и CI/CD.

Главная техническая модель сейчас:

1. Postgres backend - основной источник прикладного состояния.
2. Google Sheets - зеркало/операционный канал синхронизации, не единственный source of truth.
3. Desktop app работает с локальными файлами и backend flow.
4. FastAPI backend обслуживает web UI, imports, scans, reports, KIZ, logistics, incidents, readiness и integration queues.
5. `pending_events` - центральная durable queue для внешних side effects.
6. Workers выполняют Google sync, Telegram polling/sends, SkladBot sync/create/return, Smartup auto-import.
7. Daily SkladBot report отделен от write-capable SkladBot worker через read-only boundary.
8. Production deploy управляется GitHub Actions + VDS deploy script + Docker Compose + health/ready gates.

Ключевой операционный вывод:

- Daily SkladBot scheduled report должен отправляться только при complete coverage.
- Partial/failed/truncated/date-conflict/API/list/detail errors должны блокировать scheduled Telegram send, registry и reconciliation.
- SkladBot create/return - отдельный write-capable контур, не часть Daily SkladBot report.
- Chapman reconciliation не закрыта полностью: финальный validated discrepancy есть, но строгая order-level causal proof требует long ledger/crosswalk.

## 4. Project Inventory

| Путь | Назначение | Замечание |
|---|---|---|
| `README.md` | Основное описание проекта и текущих команд | ACTIVE, но отдельные детали требуют сверки с code truth |
| `backend/` | FastAPI backend, DB models, migrations, integrations, workers | Основной backend code truth |
| `frontend/` | React/Vite web admin | Build есть, frontend test script не найден |
| `src/taksklad/` | Desktop application modules | Tkinter/desktop flow, update, scanning, storage |
| `taksklad/` | Legacy/import package surface | Нужно сверять usage before changes |
| `tests/` | Unit/integration-style local tests | Покрывает backend, workers, desktop, reports |
| `tools/` | Release/preflight/build helper scripts | `tools/release_preflight.py` - важный release gate |
| `deploy/vds/` | Production Docker Compose/deploy/backup/restore scripts | Contains `.env` path; `.env` was not read |
| `deploy/traefik/` | Reverse proxy stack | External network `traefik` |
| `.github/workflows/` | CI, production deploy, Windows release | Manual deploy workflow exists |
| `docs/` | Runbooks, architecture, audits, historical logs | Есть docs-code drift |
| `docs/runbook/` | Operational runbooks | Этот файл создан здесь |
| `.supergoal/` | Local sanitized evidence bundles | История Daily SkladBot; not deploy input |
| `Сверка/` | Chapman reconciliation workspace | Contains real/sensitive business artifacts; raw files not used |
| `outputs/`, `reports/`, `scan_backups/`, `archive/`, `отчеты/`, `generated/` | Generated/runtime/business artifacts | Не использовать как source for graph; sensitive by default |
| `.venv/`, `frontend/node_modules/`, `frontend/dist/` | Local/generated dependencies/build output | Не deploy truth |

Sensitive/default-deny areas:

- `.env*`;
- credentials;
- raw exports;
- backups;
- real Telegram/report payloads;
- customer personal data;
- API tokens, chat ids, passwords, private keys.

## 5. Stack

| Layer | Confirmed components |
|---|---|
| Language/runtime | Python 3.12, Node 22 in CI/frontend build |
| Backend | FastAPI `0.115.6`, Uvicorn `0.34.0`, Pydantic `2.10.4` |
| DB | PostgreSQL 16 in Docker Compose, SQLAlchemy `2.0.36`, Alembic `1.14.0`, psycopg `3.2.3` |
| HTTP/API clients | `httpx 0.28.1`, requests-style integration helpers |
| Excel/data | openpyxl `3.1.5`, pandas, xlsx report generation |
| Google | gspread `6.2.1`, oauth2client |
| Desktop | Tkinter app modules under `src/taksklad` |
| Frontend | React `19.2.1`, Vite `8.0.16`, TypeScript `5.9.3`, lucide-react |
| Packaging | PyInstaller, GitHub Release assets |
| Runtime | Docker Compose, backend Dockerfile on `python:3.12-slim`, frontend Dockerfile Node build + `nginx:1.27-alpine` |
| Reverse proxy | Traefik v3.6 |
| CI/CD | GitHub Actions |

## 6. High-Level Architecture

```text
Operators
  -> Desktop app
  -> Telegram bot
  -> Web admin

Desktop/Web/Telegram
  -> FastAPI backend
  -> PostgreSQL
  -> pending_events durable queue

pending_events workers
  -> Google Sheets
  -> SkladBot
  -> Smartup
  -> Telegram
  -> XLSX reports

Deploy
  -> GitHub Actions
  -> VDS /opt/stacks/taksklad/app
  -> Docker Compose
  -> health/ready gates
```

Primary system boundaries:

- Backend API owns normalized operational state and audit/event records.
- Desktop still matters for warehouse workflows and Windows packaging.
- Web admin is operational UI over backend endpoints.
- External integrations must be treated as side-effect boundaries.
- Production/runtime truth must be checked separately from local code.

## 7. Runtime Services

Confirmed by `deploy/vds/docker-compose.yml`.

| Service | Role | Side effects |
|---|---|---|
| `postgres` | Database | Persistent data volume |
| `backend-api` | FastAPI HTTP API | DB writes, queue writes, reports, auth |
| `frontend` | Nginx-served web admin | No direct DB writes; calls backend |
| `telegram-worker` | Telegram polling, imports, notifications, daily reports | Telegram API sends/downloads, DB/queue operations |
| `skladbot-worker` | SkladBot sync/create/return processing | SkladBot write-capable create/return, DB updates |
| `smartup-auto-import-worker` | Scheduled Smartup export/import flow | Smartup export/status change, backend import, Telegram sends, optional SkladBot queue |
| `google-sheets-sync-worker` | Google pending export and optional sync worker | Google Sheets writes/reads, DB/queue operations |
| `adminer` | Optional DB admin profile | Manual DB access risk; optional profile |

Operational note:

- Compose has healthchecks.
- Backend readiness is `/ready`.
- Production deploy script checks public `/health` and `/ready`.
- Live container state was not checked in this audit.

## 8. API Surface

Confirmed from `backend/app/main.py`.

Public:

- `/health`
- `/ready`
- `/api/v1/auth/*`

Protected router:

- `/api/v1/orders/active`
- `/api/v1/admin/table`
- `/api/v1/dashboard/day-summary`
- `/api/v1/client-points`
- `/api/v1/logistics-calendar`
- `/api/v1/google/pending/retry`
- `/api/v1/events`
- `/api/v1/operations`
- `/api/v1/smartup/history`
- `/api/v1/incidents`
- `/api/v1/readiness`
- `/api/v1/orders/*`
- `/api/v1/skladbot/*`
- `/api/v1/sync/sources`
- `/api/v1/returns/*`
- `/api/v1/scans/*`
- `/api/v1/kiz/*`
- `/api/v1/imports/*`
- `/api/v1/reports/*`
- `/api/v1/logistics/*`
- `/api/v1/diagnostics/*`

Auth model observed:

- Bearer service token maps to admin-like context.
- Web sessions have role/permissions checks.
- Local-dev fallback exists only when auth config is absent.
- Admin/write dependencies protect sensitive endpoints.

Risk note:

- `/api/v1/sync/sources` can trigger Google pending export and SkladBot sync.
- Imports/scans/returns/admin actions can enqueue external side effects.

## 9. Database And Queue Model

Confirmed from `backend/app/models.py` and `backend/app/event_queue_service.py`.

Core tables/entities:

- `orders`
- `order_items`
- `scan_codes`
- `kiz_codes`
- `kiz_movements`
- `imports`
- `import_files`
- `pending_events`
- `incidents`
- `client_points`
- `logistics_calendar_days`
- `representative_contacts`
- `users`
- `audit_log`

Queue model:

- `pending_events` has event type, idempotency key, status, attempts, payload and last error fields.
- Unique idempotency index exists for queue idempotency.
- Event types cover Google export, Telegram import/notification, SkladBot create/return/daily report and other integration jobs.

Important DB constraints:

- KIZ/scanning logic must preserve deduplication and auditability.
- Event payloads can be sensitive even when API responses redact parts of data.
- DB contents were not inspected in this audit.

## 10. Backend Domain Map

| Module family | Role |
|---|---|
| `orders_service`, `order_actions_service`, `operations_service` | Active orders and order mutations |
| `scan_quantities`, `kiz_*`, `desktop_scan_rules` | Scan/KIZ business logic |
| `imports_service`, `excel_importer` | Excel/import handling |
| `reports_service`, `reconciliation_service`, `logistics_*` | XLSX/report/reconciliation/logistics |
| `event_queue_service` | Durable external side effects |
| `google_*` | Google Sheets mirror/sync/export |
| `skladbot_worker`, `skladbot_daily_report`, `skladbot_*` | SkladBot sync/write paths and daily read-only report |
| `smartup_*` | Smartup export/import/status flow |
| `telegram_worker` | Telegram polling, imports, reports and notifications |
| `health_service` | Readiness checks |
| `web_auth`, `admin_service` | Auth/admin control |
| `incidents_service`, `diagnostics_service` | Operational visibility |

## 11. Frontend Map

Confirmed from `frontend/src/App.tsx` and `frontend/src/api.ts`.

Tabs/features:

- table;
- calendar;
- clients;
- Smartup history;
- imports;
- SkladBot dry-runs;
- incidents;
- activity.

Frontend API behavior:

- Same-origin API by default.
- Uses `credentials: "include"` for web session.
- Supports Bearer token mode.
- Uses typed DTOs and AbortController timeouts.

Known gaps:

- No frontend test script was confirmed in `frontend/package.json`.
- UI quality/accessibility was not audited deeply in this run.

## 12. Desktop Map

Confirmed from repo structure and tests.

Desktop modules cover:

- app bootstrap/runtime;
- data loading;
- layout;
- day-end flow;
- order display;
- returns;
- scanning;
- updates;
- backend bridge;
- diagnostics;
- startup checks;
- storage.

Operational constraints:

- Desktop remains warehouse-critical.
- KIZ dedup, audit logs and backup behavior must be preserved.
- Windows build/release is handled by GitHub workflow and PyInstaller helpers.

## 13. External Integrations

| Integration | Current role | Write risk |
|---|---|---|
| SkladBot | Request sync, create/return queue, read-only daily report source | High for create/return worker; daily report must stay read-only |
| Smartup | Scheduled export/import, optional status changes, logistics/client reporting | High when import/status/send gates are enabled |
| Google Sheets | Mirror/pending exports and optional sync | Medium/high because spreadsheet writes affect operations |
| Telegram | Bot polling, file imports, messages, XLSX documents | High for duplicate/misleading operational sends |
| 1C/file exports | Reconciliation source family | High for data interpretation; not directly audited as live |
| GitHub Actions/Releases | CI, deploy, Windows release artifacts | High for deploy/release mistakes |
| Yandex Geocoder | Address/geocoding dependency in docs/codebase | Medium; runtime use not deeply checked here |

## 14. Daily SkladBot Current Behavior

This section is based on code truth, docs truth, tests and sanitized `.supergoal` evidence.

### 14.1 Source And Scope

Daily SkladBot report is read-only by design:

- uses `SkladBotReadOnlyClient`;
- blocks write-capable helpers such as create/update/delete/return request;
- permits only read-style endpoints required for report collection;
- separates Daily report from SkladBot create/return worker.

Primary date scope:

- unloading date or warehouse movement date is the primary operational scope;
- `created_at`/created/completed/archived dates are diagnostic, not the only report scope.

Request status handling:

- completed/archived requests can be included when they match the operational date rules;
- stale completed/archived requests must be filtered before detail lookup when outside scope.

### 14.2 Coverage And Send Gates

Coverage statuses:

- `complete`;
- `partial`;
- `failed`;
- truncation/date conflict/list/detail/API errors as blocking conditions.

Scheduled send rule:

- scheduled Telegram document is sent only if coverage is complete;
- partial/failed/truncated/date-conflict reports must not be sent automatically;
- registry write happens only after successful scheduled document send;
- reconciliation job runs only after successful scheduled send.

Manual command rule:

- manual `/skladbot_daily` blocks partial by default;
- `--allow-partial` is explicit and must mark the output as incomplete;
- manual partial does not become scheduled success and does not replace scheduled recovery.

Same-day corrected run rule:

- if a same-day scheduled event already failed or is stuck, corrected same-day collection must not silently auto-send as a normal scheduled run;
- manual recovery path must be explicit.

### 14.3 2026-07-06 / 2026-07-07 Incident Truth

Sanitized evidence indicates:

- 2026-07-06 scheduled Daily SkladBot report did not send because report coverage was partial.
- Root cause was old scheduled default `max_pages=20`, especially request type `3389`.
- Catch-up with higher page budget produced complete coverage and one explicit catch-up send.
- Registry count for catch-up became 1.
- Reconciliation was not run for that catch-up unless explicitly enabled/triggered by the scheduled success path.
- Permanent default was changed to `max_pages=60`.
- Dry-run evidence showed type `3389` natural stop around page 31/32 under the fixed default.
- Public health/ready were reported OK in sanitized artifact after the permanent fix.
- Old failed event is now expected to appear as resolved historical error instead of hot-path readiness blocker when a later success exists.

Not confirmed by this audit:

- actual 2026-07-07 22:00 scheduled run;
- operator receipt of the report;
- live DB event rows;
- live Telegram message/document state.

## 15. Smartup Auto-Import Boundary

Confirmed from `backend/app/smartup_auto_import.py`.

Default schedule in code:

- `12:00`;
- `15:00`;
- `17:50`.

Important gates:

- auto-import enabled flag;
- backend import enabled flag;
- Smartup status change enabled flag;
- process SkladBot now flag.

Blast radius:

- Smartup export;
- backend import;
- optional Smartup status change;
- queueing SkladBot work;
- Telegram report/alert sends;
- local audit/export artifacts.

Risk:

- This is a multi-side-effect chain. Any production run needs explicit permission, backup/rollback plan and stop condition.

## 16. SkladBot Write-Capable Boundary

Confirmed from SkladBot worker/request dry-run code.

Write-capable actions exist outside Daily report:

- create request;
- return request;
- stock-shortage recovery path;
- local order deletion/notification behavior in some shortage paths.

Rules:

- Daily SkladBot report must not call these paths.
- Any SkladBot create/return/write-back must be treated as production write.
- Dry-run/read-only diagnostics are allowed only when they do not call write endpoints.

## 17. Google Sheets Boundary

Confirmed from Google pending/export/sync modules.

Roles:

- mirror backend changes;
- export imports/scans/archive/returns/admin actions;
- optional sync back to backend depending on worker/config.

Risks:

- Google can become an operationally visible state even if backend is canonical.
- Pending events need idempotency and retry visibility.
- Google sync worker runtime was not verified live.

## 18. Telegram Boundary

Confirmed from `backend/app/telegram_worker.py`.

Roles:

- polling;
- file downloads/import enqueue;
- admin/manual commands;
- scheduled Daily SkladBot reports;
- other notifications/doc sends.

Risks:

- duplicate sends;
- partial report sends;
- long-running external calls delaying other Telegram worker duties;
- sensitive report documents.

Required guardrails:

- explicit complete-only gate for scheduled Daily SkladBot;
- idempotency key for scheduled reports;
- registry write only after document send success;
- no manual partial unless explicitly requested.

## 19. Chapman Reconciliation Current Truth

Based on sanitized brief in `Сверка/_reconciliation_output/CHAT_WORK_CONTEXT_BRIEF.md`.

Validated facts:

- Final inventory discrepancy for 2026-06-30: 31 blocks.
- New SKU discrepancy: 8 blocks.
- New SKU split: Green 3, Red SSL 2, Brown SSL 3.
- Old SKU final discrepancy: 23 blocks.
- Separate physical vs SkladBot/Worksheet layer: 120 blocks.
- The 120-block layer is not physical shortage by itself; physical exists, but is above SkladBot/Worksheet.
- Worksheet/storage 2026-06-30 equals SkladBot/Worksheet, not physical count.
- Aggregate formula for new SKU: `8 = -48 + 56`.

Invalid/superseded path:

- `NEW_SKU_8_ORDER_LEVEL_FINDING.md` is superseded.
- `new_sku_terminal_8_order_bridge.csv` is superseded.
- The old 4/2/2 split is wrong for final new SKU discrepancy; current split is 3/2/3.

Still unproved:

- strict order-level causal proof for all 8 new SKU blocks;
- strict bridge from SkladBot WH-R to Smartup deal id to 1C document;
- full old SKU movement explanation without SkladBot audit log.

Required next evidence:

- SkladBot audit log for old SKU movements 2026-06-29 to 2026-06-30;
- WH-R to Smartup deal id crosswalk;
- Smartup deal id to 1C document crosswalk;
- 1C register/item movement rows;
- strict long ledger keeping SkladBot daily movement date, 1C document/delivery date, physical inventory date, returns and logistics separate.

## 20. Reports And Generated Artifacts

Known report families:

- Daily SkladBot XLSX;
- day report;
- KIZ report;
- reconciliation report;
- logistics report;
- Smartup export/import artifacts;
- Windows release artifacts;
- desktop diagnostics/logs.

Daily SkladBot workbook sheets observed from code:

- summary;
- requests;
- request items;
- movements;
- stock;
- coverage/control;
- excluded requests;
- date diagnostics;
- errors.

Generated output directories are sensitive by default:

- `outputs/`;
- `reports/`;
- `scan_backups/`;
- `archive/`;
- `отчеты/`;
- `Сверка/`.

## 21. CI/CD

Confirmed workflows:

| Workflow | Trigger | Role |
|---|---|---|
| `.github/workflows/ci.yml` | push/PR to `main`, manual | backend checks, frontend build |
| `.github/workflows/deploy-production.yml` | manual `workflow_dispatch` | production deploy to VDS through SSH |
| `.github/workflows/build-windows-release.yml` | release/manual | Windows PyInstaller release assets |

CI backend job checks:

- Python 3.12;
- `compileall`;
- `unittest discover`;
- Alembic heads;
- shell syntax for deploy scripts;
- Docker Compose config.

CI frontend job checks:

- Node;
- `npm ci --prefix frontend`;
- `npm --prefix frontend run build`.

Production deploy workflow:

- validates required GitHub secrets names;
- runs local preflight checks;
- copies deploy script to VDS;
- runs VDS deploy script with selected ref/services/acceptance mode.

## 22. Production Deploy Runbook Facts

Confirmed from `deploy/vds/deploy_from_git.sh` and deploy docs.

Production app dir:

- `/opt/stacks/taksklad/app`

Deploy script behavior:

1. Checks app/env paths.
2. Rejects tracked dirty changes in the target checkout.
3. Creates restore point.
4. Runs Postgres backup.
5. Fetches/checks out requested ref.
6. Builds backend image.
7. Runs Alembic upgrade head.
8. Starts selected Docker Compose services.
9. Checks public `/health`.
10. Checks public `/ready`.
11. Optionally/required runs acceptance.
12. Scans fresh logs.

Rollback/restore helpers:

- `backup_postgres.sh`;
- `restore_postgres.sh`;
- `restore_drill.sh`;
- rollback docs in `docs/deploy-rollback-runbook.md`.

Critical risk:

- Deploy script applies migrations. Any production deploy with schema impact needs explicit migration approval, backup check, rollback path and stop condition.

Not confirmed:

- current VDS checkout;
- current VDS containers;
- current GitHub Actions runs;
- current GitHub secrets;
- current public health/ready.

## 23. Readiness And Observability

Confirmed from `backend/app/health_service.py` and runtime/deploy scripts.

`/health`:

- service liveness/status/version/environment.

`/ready`:

- DB check;
- Alembic baseline/head expectations;
- queue stale/errors;
- import errors;
- Google mirror readiness;
- Daily SkladBot historical-error resolution logic.

Observability surfaces:

- Docker healthchecks;
- backend `/ready`;
- deploy log scan;
- incidents endpoints;
- diagnostics endpoints;
- Telegram operational notifications;
- audit log table;
- `pending_events`.

Gaps:

- no live log inspection in this audit;
- no external metrics/alerting inventory confirmed;
- no live queue depth confirmed;
- no DB sample confirmed.

## 24. Security And Secrets

Secrets intentionally not read:

- local env files;
- production env files;
- credentials files;
- private keys;
- API tokens;
- Telegram chat identifiers;
- raw customer exports/reports.

Known secret-bearing configuration surfaces:

- local `.env*` files;
- `deploy/vds/.env`;
- GitHub Actions secrets;
- Telegram bot token;
- SkladBot token;
- Smartup username/password;
- Google credentials;
- DB password;
- service token;
- session secret.

Security risks/gaps from local audit:

- service token mode maps to high privilege context;
- web login rate limit is process-local;
- CSRF posture needs explicit review for cookie-auth write endpoints;
- Adminer optional profile is a sensitive operational surface;
- backend container appears to run as default root user unless hardened externally;
- backend build context lacks a dedicated `.dockerignore`;
- integration payloads in DB can contain sensitive data;
- dependency vulnerability audit was not run in this pass.

## 25. Risk Register

| Priority | Risk | Current control | Gap / next action |
|---|---|---|---|
| P0 | Production write without explicit boundary | AGENTS/read-only default, deploy scripts, workflow gates | Keep explicit permission requirement for deploy/migration/write |
| P0 | SkladBot write endpoint called from report path | `SkladBotReadOnlyClient`, tests | Preserve boundary in every future Daily report change |
| P0 | Telegram scheduled sends partial Daily report | Complete-only gate and tests | Observe next scheduled run live |
| P0 | Duplicate/misleading Telegram Daily report | idempotency key, registry after send | Live DB/message state not verified |
| P0 | Alembic migration during deploy changes production data/schema | deploy backup before upgrade | Require migration plan/rollback/stop condition |
| P0 | Smartup auto-import chain writes too broadly | config gates | Treat each run as production write |
| P0 | KIZ dedup/audit corruption | DB models/tests/domain logic | Full KIZ regression needed before broad scan changes |
| P0 | Secret leak into docs/logs/reports | deny reading secrets, marker scan | Keep generated outputs out of docs/graph |
| P1 | SkladBot pagination/truncation | default `max_pages=60`, coverage flags | Monitor high-volume days |
| P1 | SkladBot movement/products/stock truncation | coverage warnings/errors | Need live high-volume observation |
| P1 | Date conflict around unloading/movement/created dates | diagnostics and coverage gate | Continue separating date layers |
| P1 | Manual partial report misuse | `--allow-partial` explicit | Operator training/runbook clarity |
| P1 | Same-day corrected scheduled run ambiguity | manual recovery marker | Needs clear incident runbook use |
| P1 | Registry/reconciliation out of sync | registry/recon only after scheduled success | Live DB confirmation needed |
| P1 | Dirty worktree hides release truth | git status visible | Release/deploy must use clean target/ref |
| P1 | Docs-code drift | this audit identifies drift | Update active docs after code truth is accepted |
| P1 | Chapman aggregate proof mistaken for causal proof | superseded docs identified | Build strict long ledger/crosswalk |
| P1 | Missing SkladBot audit/crosswalk evidence | blockers listed | Get audit log + WH-R/deal/1C mapping |
| P1 | `/ready` hardcoded migration head | visible in health service | Update readiness with each migration |
| P1 | Telegram worker combines many duties | single worker loop | Consider isolation/time budgets if delays recur |
| P2 | Frontend has no confirmed tests | build only in CI | Add focused tests if UI behavior grows |
| P2 | Backend container hardening | Docker defaults | Consider non-root/cap_drop/read_only where compatible |

P0/P1 count in this audit: 20.

## 26. Docs-Code Drift

Confirmed drift areas:

- `docs/taksklad-system-stack-overview.md` is useful but dated 2026-06-30 and references older version context.
- `docs/README.md` has status wording that can lag current code version.
- `backend/README.md` still contains older MVP/not-production-style wording in places.
- `docs/runbook/daily-workers-skladbot-audit.md` is partly superseded by later Daily SkladBot fixes.
- Older changelog/implementation notes mention earlier created-date behavior and incident states.
- `docs/taksklad-full-technical-audit-2026-07-04.md` is valuable historical audit but predates latest Daily SkladBot max-pages/readiness closeout.
- Manual acceptance docs may mention older production path `/opt/taksklad/app`; current deploy path is `/opt/stacks/taksklad/app`.

Rule:

- When docs conflict with code/runtime, use docs as intent/history, then verify code truth and live truth separately.

## 27. Operational Runbooks

### 27.1 Daily SkladBot Scheduled Check

Goal: verify scheduled complete-only behavior without accidental sends.

Read-only verification steps:

1. Check public `/health`.
2. Check public `/ready`.
3. Inspect recent Daily SkladBot event state read-only.
4. Confirm coverage status.
5. Confirm whether a Telegram document was sent only if coverage was complete.
6. Confirm registry entry exists only after document send success.
7. Confirm reconciliation ran only after scheduled success if enabled.
8. Confirm no SkladBot write endpoints were called by daily report path.

Stop condition:

- Any partial/failed/truncated/date-conflict result must stop scheduled send.

### 27.2 Daily SkladBot Catch-Up

Goal: recover a missed report without hiding the incident.

Required boundaries:

- explicit date;
- explicit permission for any send;
- dry-run/coverage first;
- no SkladBot writes;
- no reconciliation unless explicitly intended;
- clear registry behavior.

Stop condition:

- partial coverage, API errors, truncation or unresolved date conflicts.

### 27.3 Production Deploy

Required before deploy:

1. Confirm requested ref.
2. Confirm no unrelated worktree changes in release target.
3. Confirm tests/preflight.
4. Confirm backup path.
5. Confirm migration impact.
6. Confirm selected services.
7. Confirm acceptance mode.
8. Confirm rollback path and stop condition.

Stop condition:

- failed preflight;
- migration risk without approval;
- missing backup;
- failed `/health` or `/ready`;
- fresh critical logs.

### 27.4 Chapman Reconciliation Continuation

Goal: prove discrepancy causality without reusing superseded bridge.

Steps:

1. Use current validated totals: 31 total, 8 new SKU, 23 old SKU.
2. Preserve new SKU split 3/2/3.
3. Do not use superseded 4/2/2 bridge.
4. Build strict long ledger.
5. Build WH-R to Smartup deal id crosswalk.
6. Build Smartup deal id to 1C document crosswalk.
7. Keep movement, delivery, physical inventory, return and logistics dates separate.

Stop condition:

- any arithmetic bridge that cannot tie exact identifiers across systems.

## 28. Open Questions

Live/runtime:

- What is current production `/health` and `/ready`?
- What are current Docker service states?
- Are Daily SkladBot events clean after the next scheduled 22:00 run?
- Are there current stale or failed `pending_events`?
- Did the operator receive the expected Daily report?

Data:

- What are current DB counts for relevant Daily SkladBot event rows?
- Are registry/reconciliation rows consistent with actual Telegram sends?
- Are Google pending exports clear?

Chapman:

- Where is SkladBot audit log for old SKU 2026-06-29 to 2026-06-30?
- What is the authoritative WH-R to Smartup deal id map?
- What is the authoritative Smartup deal id to 1C document map?
- Which 1C register rows explain the remaining old SKU layer?

Security:

- Is CSRF protection sufficient for cookie-auth writes?
- Are service tokens scoped enough?
- Are container hardening defaults acceptable for production?
- Is dependency vulnerability scanning enabled elsewhere?

Docs:

- Which older docs should be marked historical/superseded?
- Should `docs/runbook/daily-workers-skladbot-audit.md` be rewritten to match current code?

## 29. Recommended Next Steps

Immediate:

1. Observe the next scheduled Daily SkladBot run read-only.
2. Confirm `/health` and `/ready` after that run.
3. Confirm no partial scheduled send happened.
4. Confirm registry/reconciliation behavior matches code rules.
5. Update/mark stale Daily SkladBot docs after live confirmation.

Short term:

1. Run full local test suite on a clean test pass.
2. Run `tools/release_preflight.py --skip-network`.
3. Run frontend build if deploy readiness is needed.
4. Review docs-code drift and mark superseded docs.
5. Add/verify `.dockerignore` for backend build context if needed.

Medium term:

1. Add focused docs for Daily SkladBot incident/catch-up/recovery.
2. Add explicit CSRF/security review for cookie-auth write endpoints.
3. Separate or time-box long Telegram worker external calls if delays recur.
4. Build Chapman long ledger and crosswalk.

Long term:

1. Add dependency vulnerability scanning in CI if not handled elsewhere.
2. Consider container hardening after compatibility check.
3. Add more frontend tests for high-risk operational UI.
4. Improve live observability around `pending_events`, Daily report outcomes and worker lag.

## 30. Audit Commands Run

| Command | Result | Notes |
|---|---:|---|
| `git branch --show-current` | 0 | `main` |
| `git status --short --branch` | 0 | Dirty worktree existed before this audit |
| `git rev-parse HEAD` | 0 | `c7b3ecffda55ff3ae7ff4e3bc8b2edebe5c06866` |
| `/Users/anton/Documents/work/_knowledge-graph/scripts/graph-query.sh TakSklad "полный аудит архитектура сервисы воркеры интеграции deploy runbook"` | 0 | Graph used only as routing hint, not source of truth |
| `find . -maxdepth 2 -type d \| sort` | 0 | Project inventory collected |
| `find docs/runbook -maxdepth 2 -type f` | 0 | Existing runbook folder inspected |
| `find .supergoal -maxdepth 2 -type f` | 0 | Sanitized evidence files discovered |
| `rg` over backend/tests/docs/deploy/workflows for Daily/report/ready/worker patterns | 0 | Large output; key files inspected directly |
| `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_skladbot_daily_report` | 0 | 74 tests OK |
| `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_telegram_import` | 0 | 71 tests OK |
| `PYTHONPATH=. ./.venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py backend/app/telegram_worker.py` | 0 | Compile OK |
| `git diff --no-index --check /dev/null docs/runbook/FULL_PROJECT_AUDIT_ARCHITECTURE_RUNBOOK.md` | 0 | No whitespace errors in new untracked audit file |
| `rg` high-confidence secret-marker scan on this audit file | 0 | No matches |
| `rg -n '[ \t]+$' docs/runbook/FULL_PROJECT_AUDIT_ARCHITECTURE_RUNBOOK.md` | 0 | No trailing whitespace |
| `git status --short -- docs/runbook/FULL_PROJECT_AUDIT_ARCHITECTURE_RUNBOOK.md docs/runbook` | 0 | New audit file is untracked; existing `daily-workers-skladbot-audit.md` is also untracked/preexisting |

Additional subagent read-only checks reported:

- backend/API/DB/queue/integration code map;
- CI/CD/deploy/runtime config map;
- docs/Daily SkladBot/Chapman artifact map.

## 31. Final Non-Actions Confirmation

In this audit:

- production write: no;
- deploy: no;
- restart: no;
- migration: no;
- DB write: no;
- Telegram send: no;
- SkladBot write: no;
- Smartup write/status change: no;
- Google Sheets write: no;
- secret file read: no;
- raw client export/report read: no;
- commit: no;
- push: no.

## 32. Status

Confirmed:

- repo branch and dirty status;
- local architecture;
- backend/API/workers/queue model;
- deploy scripts and CI workflows;
- Daily SkladBot code/test/docs behavior;
- Chapman sanitized current facts;
- two relevant Daily SkladBot unittest suites;
- compile check for Daily SkladBot and Telegram worker files.

Partially confirmed:

- production readiness from local deploy scripts and runbooks.

Not confirmed:

- live VDS state;
- live public health/ready at audit end;
- live DB rows;
- live Telegram sends/documents;
- live SkladBot/Smartup/Google state;
- GitHub Actions current run state;
- operator receipt and physical warehouse truth.

Next required verification:

- separate read-only live runtime/API/DB smoke with explicit permission and no writes.
