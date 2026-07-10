# TakSklad disaster recovery runbook

## Truth boundary

- The repository and synthetic drills prove the procedure and its fail-closed gates.
- A production backup is not proven until its archive, SHA256, manifest and encrypted
  off-host object have all been verified for the same backup ID.
- A weekly isolated drill must not connect to, stop or mutate production services.
- A production restore remains operator work and is forbidden without the exact
  approval described below.

## Targets

- RPO: at most 15 minutes, enforced by PostgreSQL `archive_timeout=900s` and a
  point-in-time drill against synthetic WAL evidence.
- RTO: at most 30 minutes on the documented synthetic profile.
- Daily logical backup plus WAL archive; encrypted off-host retention is a separate
  failure domain from the VPS.

## Backup evidence

Every completed backup has one ID and three atomically published files:

- `<backup-id>.dump` (current custom archive) or a validated legacy `.sql.gz`;
- `<backup-id>.sha256`;
- `<backup-id>.manifest.json`.

The manifest is sanitized. It may contain IDs, hashes, sizes, timestamps, format,
migration revision, row counts and elapsed milliseconds. It must never contain row
values, customer names, KIZ values, tokens, connection strings or file contents.
Files with `.partial` suffix are incomplete and must never be restored or uploaded.

## Weekly isolated drill

Run the drill only against a disposable database/container and a verified backup.
Capture the migration head, aggregate table counts, invariant result, readiness
result and elapsed time. Compare production state fingerprints before and after the
drill; they must be identical. Do not reuse a production database name, volume or
network.

## Production restore stop condition

Before any destructive database action:

1. Identify `<backup-id>` and verify archive, manifest and SHA256 locally and against
   the encrypted off-host object.
2. Record current service state and drain/stop every writer: backend API, Telegram,
   Google Sheets, SkladBot and Smartup workers.
3. Confirm there are no active application sessions/writers.
4. Create and verify a pre-restore backup.
5. Obtain the exact one-line approval:

   `APPROVE_TAKSKLAD_PRODUCTION_RESTORE <backup-id> <sha256>`

Any mismatch, missing writer drain, failed pre-restore backup, invalid archive,
unknown migration head or readiness failure is a hard stop. `CONFIRM_RESTORE=YES`
is obsolete and is not sufficient.

## Restore sequence after approval

1. Re-verify the selected archive and exact approval value.
2. Keep all writers stopped while schema/data changes run.
3. Restore into a disposable validation database first; require migration head,
   counts, invariants and readiness to pass.
4. Restore production, run forward migrations only, then repeat counts, invariants
   and readiness.
5. Start backend API first, confirm readiness, then start workers one at a time.
6. Require operator checks for the warehouse workflow before declaring recovery.

Database downgrade is not part of this procedure. If any post-restore check fails,
keep writers stopped and use the verified pre-restore backup under a new explicit
approval; do not improvise a partial restore.

## Evidence classification

- Code truth: scripts and exact approval parser.
- Test truth: synthetic backup, restore, off-host and PITR commands.
- Live truth: production backup/off-host freshness and production service state.
- Operator truth: warehouse acceptance after a real restore.

Phase 24 establishes code/test truth only. Production/live/operator proof remains an
explicit approval-gated operation.
