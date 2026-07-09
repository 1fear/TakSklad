# Event Queue Lifecycle

TakSklad uses `pending_events` as the durable queue for side effects that must survive worker restarts: Google Sheets mirror exports, SkladBot create/return work, Telegram imports, Telegram notifications, and scheduled reports.

## Lifecycle

| State | Meaning | Retry behavior | Terminal |
|---|---|---|---|
| `pending` | Event is waiting for a worker. | Worker may claim it. | No |
| `processing` | Worker has claimed the event and incremented `attempts`. | If `updated_at` is stale, worker resets it to `pending` and audits the reset. | No |
| `failed` | Last attempt failed, but the event is retryable/manual-reviewable. | Eligible workers may retry it if their policy allows. | No |
| `waiting_shipment_date` | Telegram import waits for the user to enter the shipment date. | Not claimed by import worker until date is set. | No |
| `waiting_date_choice` | Telegram import waits for user confirmation after Excel/Telegram date conflict. | Not claimed by import worker until user chooses. | No |
| `completed` | External side effect finished or was safely skipped. | Not retried. | Yes |
| `blocked` | Event/order is intentionally blocked by business logic. | Not retried automatically. | Yes |
| `cancelled` | User cancelled the operation. | Not retried. | Yes |
| `dead` | Reserved for future hard dead-letter policy. | Not retried automatically. | Yes |

## Claiming And Locking

Workers claim active queue events with `FOR UPDATE SKIP LOCKED` when the database is PostgreSQL. Google Sheets pending exports also use a process-local lock on non-Postgres databases, so SQLite/local runs return `busy` instead of processing the same queue twice in one process. SQLite/local tests use the same lifecycle rules.

Owner-scoped leases are available behind `TAKSKLAD_EVENT_LEASES_ENABLED=1` for a controlled canary. Google exports, SkladBot create/return and queued Telegram notifications claim a whole batch atomically through `UPDATE ... RETURNING`, commit every owner/expiry before the first external call, and finalize only with the matching unexpired owner. The rollback value is `0`/unset; active leases are never reset by the legacy path. Delivery remains durable at-least-once, not exactly-once.

Retry eligibility uses the indexed `available_at` column. `lease_expires_at` is the only lease recovery clock; non-expired owners remain untouched. Authorized queue diagnostics expose bounded oldest-ready age, active leases, expired leases and scheduled retry backlog.

## Retry And Rate Limits

Google Sheets export pauses the batch on rate-limit errors such as `429`, `quota`, or `rate limit`, keeps the event `pending`, records `last_error`, and writes `payload.next_attempt_at`. Pending exports whose `next_attempt_at` is in the future are skipped until cooldown expires, but they do not block newer ready exports. SkladBot API calls respect `Retry-After` in their client retry loop.

If backend import succeeds but queuing the Google Sheets import export fails, the import remains committed in PostgreSQL. The API returns the import result with `google_sheets_status=error`, writes `google_sheets_import_export_failed` audit, and opens a `google_sheets_import_export` incident for operator review.

Manual admin retry is allowed only for retryable event types in `failed` or `pending` state. The request must include a non-empty reason, clears `next_attempt_at`, stores retry metadata in the event payload, and writes `pending_event_retry_requested` to audit.

Telegram Excel import retry requires the original Telegram document source in `payload.document.file_id`. If the file id is missing, the API returns `409` and leaves the event unchanged, because the worker cannot redownload the original file safely.

Telegram notification events with malformed payload are blocked, not failed: missing `payload.text` or missing resolved target chat moves the event to `blocked`, records `last_error`, and writes `telegram_notification_blocked` to audit. Real Telegram send exceptions remain `failed` and retryable.

## Diagnostics

`/api/v1/admin/events` exposes queue status, attempts, idempotency key, next attempt, last error, and stale processing events.

Authenticated event diagnostics expose `raw_payload` for operator review only after backend redaction. Secret-like keys such as token/password/secret/authorization are masked, and secret-looking strings inside `last_error` or payload values are redacted before they reach the web UI.

`/api/v1/diagnostics/logs` includes the same active queue fields in the downloadable support log with secret redaction.

Public `/ready` uses a narrower view than authenticated admin diagnostics: it exposes only counts and policy status, without event identifiers or error text. Any unresolved failed/error/blocked mandatory hot-path event makes readiness unhealthy with HTTP 503 even when `last_error` is empty. Google Sheets is an optional mirror: its isolated degradation is explicit as top-level `status=degraded`, but keeps `ready=true` and HTTP 200.

## Safety Rules

- Bad Google export events are marked `failed` and do not stop newer valid events in the same batch.
- Stale Telegram import, Telegram notification, and scheduled report events are reset from `processing` to `pending`.
- State-store events such as chat state may appear in authenticated queue summaries, but public readiness must expose only aggregated type/status counts.
