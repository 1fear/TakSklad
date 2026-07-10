# Operational observability

The private `GET /api/v1/admin/metrics` endpoint uses the existing diagnostics authorization policy. It is intentionally inside the authenticated API router and has `Cache-Control: no-store`. Traefik does not expose a separate public metrics path.

`monitoring/observability/signal-catalog.json` is the source of truth for bounded labels. Order, client, KIZ, address, phone, credential, correlation and request identifiers must never become metric labels. Correlation IDs are allowed only as sanitized log fields.

`dashboard.json` covers request p50/p95/p99, 5xx rate, queue pickup and age, DB pool utilization, backup freshness and restore-drill freshness. `alert-rules.json` covers readiness, 5xx, latency, stale workers, queue age, provider failures, backup age and drill age. Queue pickup uses only rows currently in `processing`: because the schema has no immutable `claimed_at`, `updated_at - created_at` is a conservative latest-claim/progress proxy. Completed rows are excluded because their `updated_at` is completion time.

`taksklad_runtime_identity_valid` has no labels and reports only whether exact commit/image/version values are present. This avoids unbounded build identifiers in metric labels. The exact tuple is proved by `tools/check_runtime_identity.py` against HTTP `/health` in a disposable local image.

Queue, import, provider and worker signals are calculated from bounded recent database rows. Backup and restore-drill freshness is read only from `/run/taksklad-observability/maintenance.json`, with the exact fields `backup_success_at` and `restore_drill_success_at`. The collector must atomically replace that file and mount it read-only into the API container. Missing, oversized or malformed markers produce an epoch-age value and therefore cannot appear falsely fresh.

`tools/run_alert_smoke.sh` is local-only: it accepts only `--synthetic-only`, emits real registry snapshots, evaluates them through the alert state machine, and writes observed firing/recovery transitions only to a temporary local JSONL file. It has no network or external sender implementation. Production monitoring installation and alert routing remain an approval-gated operation.
