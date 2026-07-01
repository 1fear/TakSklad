# TakSklad Backend MVP

This directory contains the first VDS-ready backend skeleton for TakSklad.

Current scope:

- FastAPI application shell.
- PostgreSQL connection settings.
- Initial API contracts for health, active orders, scans, imports, and day reports.
- Initial PostgreSQL schema SQL.
- Docker image definition.
- Alembic migration baseline for controlled schema upgrades.

This is not a production release yet. The desktop app still works directly with Google Sheets until the backend is deployed, verified, and connected behind feature flags.

## Local Docker Run

From repository root:

```bash
cp deploy/vds/.env.example deploy/vds/.env
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build
```

When Traefik is configured on VDS, check through the configured domain:

```bash
curl https://$TAKSKLAD_BACKEND_HOST/health
```

The compose file does not publish PostgreSQL or the backend API directly to the public internet. HTTP traffic should go through Traefik.

## Database Migrations

Alembic config lives in `backend/alembic.ini`, with revisions in `backend/migrations/versions`.

For local development, run Alembic only against a local or copied database. For the existing VDS database, first create a backup and baseline-stamp the already deployed schema instead of running the baseline migration as DDL.

Full migration procedure: `docs/database-migrations-runbook.md`.

## API Status

Implemented now:

- `GET /health`
- `GET /ready`
- `GET /api/v1/readiness`
- `GET /api/v1/orders/active`
- `POST /api/v1/scans`
- `POST /api/v1/orders/{order_id}/complete`
- `POST /api/v1/imports`
- `GET /api/v1/imports`
- `GET /api/v1/reports/day`
- `GET /api/v1/admin/client-points`
- `POST /api/v1/admin/client-points/timeslot`

No contract placeholders remain in the backend API MVP.

## Health And Readiness

`GET /health` is lightweight liveness. It does not touch PostgreSQL, Google Sheets, SkladBot, or Telegram.

`GET /ready` is internal readiness for VDS checks. It pings PostgreSQL and reports Alembic revision, queue backlog by type/status, oldest pending age, stale processing count, and sanitized recent errors. Queue event types with dynamic suffixes are aggregated as `prefix:*`, and compact error rows do not expose raw payloads, idempotency keys, or linked entity fields.

`GET /api/v1/readiness` returns the same readiness payload behind the normal API auth/session guard.

## API Auth

`GET /health` remains public. Protected `/api/v1/*` endpoints accept either a configured Bearer service token or a valid web session cookie.

An empty `TAKSKLAD_API_TOKEN` disables only the Bearer-token path. It does not open protected API routes when web auth is configured. Local no-auth mode is allowed only when neither `TAKSKLAD_API_TOKEN` nor web auth credentials are configured.

Web sessions include `role` and `permissions`. The env-configured web login is treated as `admin`; DB-backed `users` rows with role `logistics_slots` can read the web UI and write only client-point delivery slots. State-changing warehouse/admin endpoints require `admin:write`; `POST /api/v1/admin/client-points/timeslot` requires `client_points:write`.

The frontend same-origin `/api/` proxy must forward browser cookies without injecting the internal service token. Otherwise web sessions would be upgraded to service/admin at the proxy layer.

## SkladBot SKU Mapping

SkladBot request/return payloads use the built-in Chapman SKU mapping by default. On VDS, `SKLADBOT_SKU_MAPPING_JSON` can override or add SKU keys such as `red:op`:

```json
{"red:op":{"product_data_id":2189390,"barcode":"4006396053947","is_main_barcode":false}}
```

If this JSON is invalid or an entry misses `product_data_id`, `barcode`, or boolean `is_main_barcode`, the dry-run blocks the affected order and no SkladBot create event is queued.

## SkladBot Representative Contacts

TakSklad stores sales representative phone numbers in `representative_contacts`. SkladBot request comments keep the payment type on the first line, then the representative, then available work/personal phone numbers:

```text
Терминал
ТП-1 Умид
Рабочий номер: +998 91 111 11 11
Личный номер: +998 90 222 22 22
```

Load the local XLSX reference into the configured backend database with:

```bash
PYTHONPATH=. python tools/import_representative_contacts.py "/path/to/номера тп.xlsx"
```

Use `--dry-run` to validate the workbook without committing. The script reports only row counts and does not print phone values.

## Day Report

`GET /api/v1/reports/day?report_date=YYYY-MM-DD`

Builds a PostgreSQL-based day summary:

- orders for the selected order date;
- orders scanned on the selected date;
- planned/scanned/remaining blocks;
- payment groups;
- SkladBot request number if imported with the order.

## Client Points And Logistics Slots

`GET /api/v1/admin/client-points` returns saved delivery points plus legal entities already seen in orders.

`POST /api/v1/admin/client-points/timeslot` creates or updates a saved point by `client_name`. In this flow the client/legal entity name is the point identity; address, coordinates and representative are mutable details refreshed from newer imports.

The web `Клиенты` tab uses the same endpoint for manual point creation, inline delivery-window edits, and resetting a custom slot back to `10:00-18:00`.

The logistics XLSX keeps the default `10:00-18:00` window for unknown clients and uses the saved `client_points.delivery_from/delivery_to` values when a matching client exists.
