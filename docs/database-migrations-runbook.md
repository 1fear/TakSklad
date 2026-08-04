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

Revision `20260710_0010` makes the warehouse data assumptions enforceable in PostgreSQL. It adds validated checks for nonnegative and internally consistent quantities, supported legacy statuses, import row counts, pending-event attempts, and complete/nonblank materialized identities. It also adds order-scoped active import identity indexes. Returned orders remain reusable history and therefore do not participate in the active-order unique index.

Run the read-only preflight before scheduling this migration:

```bash
./tools/check_data_invariants.sh --database-url "$DATABASE_URL" --read-only
```

The preflight only counts violations in a repeatable-read, read-only transaction. It does not repair, delete, merge, or execute DDL. Any nonzero invariant count is a hard stop: keep the database at `20260710_0009`, investigate the reported class, and use a separately reviewed forward data repair. The migration uses `NOT VALID` checks followed by validation, two-second lock timeouts, bounded statement timeouts, and concurrent unique-index creation. A lock timeout or validation failure must leave the previous head active; retry only after the blocker or data violation is resolved. Production execution still requires a verified backup, an approved maintenance window, and explicit production authorization.

Revision `20260710_0011` expands `pending_events` with nullable `action`, `aggregate_type`, and `aggregate_id` columns, backfills legacy rows from their existing payload, and adds a concurrent composite lookup index. Column creation and invalid-index recovery are retry-safe if the concurrent step is interrupted after the expand transaction commits. Producers dual-write the normalized identity and the legacy payload keys, so current consumers remain compatible. The application owns the transaction: warehouse mutation, audit row, and all external intents commit together; consumers retain honest at-least-once delivery and must remain idempotent.

## Shipping a Migration to Production

The server deploy refuses schema drift by default. A release that adds Alembic revisions
ships only when two independent conditions hold, so neither a stray build nor a stray
deploy can move the schema on its own.

1. Build the server release with `database_migration_policy: forward_upgrade`.
   The value is written into the signed release manifest, so the deploy verifies a policy
   that was chosen at build time and attested, not one typed at deploy time.
   A schema-stable release keeps the default `no_change`.
2. Run the server deploy with `database_migration_approval: APPLY_FORWARD_DATABASE_MIGRATION`.

The deploy then cross-checks reality against the declaration and stops on every mismatch:

| Situation | Result |
|-----------|--------|
| Migrations changed, manifest says `no_change` | `SERVER_RELEASE_DATABASE_MIGRATION_DIFF_FORBIDDEN` |
| Migrations changed, manifest says `forward_upgrade`, approval missing or wrong | `SERVER_RELEASE_DATABASE_MIGRATION_APPROVAL_REQUIRED` |
| Manifest says `forward_upgrade` but no migration actually changed | `SERVER_RELEASE_FORWARD_UPGRADE_WITHOUT_MIGRATION_DIFF` |
| `forward_upgrade` while production already sits at the release head | `PRODUCTION_ALEMBIC_HEAD_ALREADY_AT_FORWARD_UPGRADE_RELEASE` |
| Candidate image head differs from the manifest head | `CANDIDATE_ALEMBIC_HEAD_DIFFERS_FROM_RELEASE` |

Applying the migration remains the job of `deploy/vds/deploy_from_git.sh`, which already
quiesces the writer services, takes the cutover backup, runs `alembic upgrade head` from
the verified image, and rolls the runtime back on any failure. Nothing in this path performs
a downgrade: rollback restores the previous image digests and retains the schema.

`destructive_migrations_allowed` and `alembic_downgrade_allowed` stay `false` under both
policies. `forward_upgrade` widens *when* a migration may run, never *what* it may do.

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
