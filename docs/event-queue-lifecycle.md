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

Workers claim active queue events with `FOR UPDATE SKIP LOCKED` when the database is PostgreSQL. SQLite/local tests use process-local execution and the same lifecycle rules.

## Retry And Rate Limits

Google Sheets export pauses the batch on rate-limit errors such as `429`, `quota`, or `rate limit`, keeps the event `pending`, records `last_error`, and writes `payload.next_attempt_at` for diagnostics. SkladBot API calls respect `Retry-After` in their client retry loop.

## Diagnostics

`/api/v1/admin/events` exposes queue status, attempts, idempotency key, next attempt, last error, and stale processing events.

`/api/v1/diagnostics/logs` includes the same active queue fields in the downloadable support log with secret redaction.

## Safety Rules

- Bad Google export events are marked `failed` and do not stop newer valid events in the same batch.
- Stale Telegram import, Telegram notification, and scheduled report events are reset from `processing` to `pending`.
- State-store events such as chat state may appear in queue summaries, but only active queue statuses are shown in the failed/pending diagnostics section.
