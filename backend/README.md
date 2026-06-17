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

No contract placeholders remain in the backend API MVP.

## Health And Readiness

`GET /health` is lightweight liveness. It does not touch PostgreSQL, Google Sheets, SkladBot, or Telegram.

`GET /ready` is internal readiness for VDS checks. It pings PostgreSQL and reports Alembic revision, queue backlog by type/status, oldest pending age, stale processing count, and sanitized recent errors.

`GET /api/v1/readiness` returns the same readiness payload behind the normal API auth/session guard.

## Day Report

`GET /api/v1/reports/day?report_date=YYYY-MM-DD`

Builds a PostgreSQL-based day summary:

- orders for the selected order date;
- orders scanned on the selected date;
- planned/scanned/remaining blocks;
- payment groups;
- SkladBot request number if imported with the order.
