-- Phase 3 invariant preflight.
-- Run read-only before adding any future uniqueness constraint around KIZ scans/events.

-- Same item must not contain the same KIZ twice. Rows returned here need manual cleanup
-- or a forward data-repair migration before a future UNIQUE(order_item_id, code) index.
SELECT
    order_item_id,
    code,
    COUNT(*) AS duplicate_count,
    ARRAY_AGG(id ORDER BY scanned_at, id) AS scan_code_ids
FROM scan_codes
GROUP BY order_item_id, code
HAVING COUNT(*) > 1;

-- Reuse across different items is allowed only when KIZ movement history contains a return/reset/undo
-- before the later outbound. This query is diagnostic; it is not a constraint candidate.
SELECT
    code,
    COUNT(*) AS scan_count,
    COUNT(DISTINCT order_item_id) AS item_count
FROM scan_codes
GROUP BY code
HAVING COUNT(DISTINCT order_item_id) > 1;

-- Pending event idempotency keys must stay unique before relying on DB conflict handling.
SELECT
    idempotency_key,
    COUNT(*) AS duplicate_count,
    ARRAY_AGG(id ORDER BY created_at, id) AS event_ids
FROM pending_events
WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
GROUP BY idempotency_key
HAVING COUNT(*) > 1;

-- KIZ code registry is expected to be unique.
SELECT
    code,
    COUNT(*) AS duplicate_count,
    ARRAY_AGG(id ORDER BY first_seen_at, id) AS kiz_code_ids
FROM kiz_codes
GROUP BY code
HAVING COUNT(*) > 1;
