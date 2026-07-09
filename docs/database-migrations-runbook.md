# Database Migrations Runbook

TakSklad uses PostgreSQL as the source of truth. Schema changes after this point must go through Alembic migrations.

## Local Check

From the repository root:

```bash
cd backend
DATABASE_URL=postgresql+psycopg://taksklad:taksklad@localhost:5432/taksklad alembic -c alembic.ini current
DATABASE_URL=postgresql+psycopg://taksklad:taksklad@localhost:5432/taksklad alembic -c alembic.ini upgrade head
```

Use a local or copied database only. Do not point this command at production during development.

## Existing Production Database

For the current live VDS database, the first Alembic action is a baseline stamp, not `upgrade head`.

1. Create a fresh PostgreSQL backup.
2. Verify that the live schema already has the effective baseline tables: `orders`, `order_items`, `scan_codes`, `kiz_codes`, `kiz_movements`, `pending_events`, `import_files`, `audit_log`.
3. Run the stamp once from a controlled VDS shell:

```bash
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml exec -T backend-api \
  alembic -c alembic.ini stamp 20260616_0001
```

After the stamp, future schema changes use new Alembic revisions and `alembic upgrade head`.

## Invariant Preflight

Before adding future uniqueness constraints for KIZ scans or pending-event idempotency, run:

```bash
set -a
. deploy/vds/.env
set +a
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml exec -T postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < backend/sql/preflight_phase3_invariants.sql
```

Phase 3 deliberately does not add a global unique constraint on `scan_codes.code`, because returned KIZs must be reusable for future outbound shipments. Cross-order protection is enforced by KIZ movement history plus a PostgreSQL transaction advisory lock per normalized KIZ code. A future `UNIQUE(order_item_id, code)` index is allowed only after the same-item duplicate query returns no rows.

## Legacy SQL Files

`backend/sql/001_initial_schema.sql` and `backend/sql/002_kiz_movements.sql` remain only as historical recovery inputs. A normal empty database is created exclusively with `alembic upgrade head`; Compose no longer mounts raw SQL into `docker-entrypoint-initdb.d`.

`deploy/vds/apply_schema.sh` is fail-closed behind the exact local flag `TAKSKLAD_LEGACY_SQL_BOOTSTRAP=ALLOW_EMPTY_UNVERSIONED_DATABASE_ONLY`. It also rejects any database with an Alembic version table or existing application tables. Use it only for a separately reviewed legacy recovery; never combine it with Alembic baseline creation.

## Rollback Posture

The baseline migration is irreversible by design. Production rollback means restore a PostgreSQL backup or ship a new forward repair migration. Do not edit a migration that has already run or been stamped in production.
