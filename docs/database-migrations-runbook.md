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

Production activation is fail-closed: after `upgrade head` the deploy script performs a read-only comparison of the single `alembic current` value with the single `alembic heads` value. Missing, stale or multiple revisions stop activation, and `/ready` returns HTTP 503 until the database is at the exact current head.

Revision `20260710_0008` adds the pending-event lease columns and claim/expiry indexes. Existing `processing` rows are marked as expired legacy leases so they are recoverable; no live event is deleted. Roll back operationally by disabling `TAKSKLAD_EVENT_LEASES_ENABLED`, not by downgrading this forward-only migration.

Revision `20260710_0009` is an expand-only import-identity migration. It adds nullable, indexed lookup keys while preserving legacy JSON unchanged:

- `orders.import_order_key` is the resolved order identity; `import_source_order_key` retains the pre-split source identity.
- `order_items.source_import_id` retains the exact source value, while `source_import_key` is its fixed SHA-256 lookup key.
- If `source_import_id` is empty, `order_items.import_item_key` is the active item identity. `source_batch_key` remains provenance.
- Completed orders remain active for dedup. Returned orders are immutable history and do not block reimport; both `orders.status` and legacy `raw_payload.return_status` are checked.
- Late Smartup rows linked to an existing SkladBot request keep using the stable `late-skladbot-split:*` identity. No global unique key is added because returned history may legitimately reuse the same source identity.
- A SHA-256 identifies one `ImportFile`, not one delivery group: Smartup can create several group imports from the same workbook. Replays are serialized by the file lock, create an ImportJob linked through `file_sha256_reused_from_import_id`, then return an idempotent row outcome (`items_created=0`, duplicate counted) without a raw uniqueness error.

Before any later production apply, run the bounded preflight first and stop on every conflict; never merge or delete automatically:

```bash
PYTHONPATH=. .venv/bin/python tools/import_identity_backfill.py \
  --dry-run --database-url "$DATABASE_URL" --batch-size 500
```

Resume with the reported `next_after_order_id` and `next_after_item_id` if an operational window ends. `--apply` is allowed only after a complete dry-run reports zero conflicts and an approved backup/rollback gate exists.

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
