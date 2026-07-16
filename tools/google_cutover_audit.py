#!/usr/bin/env python3
"""Read-only audit of legacy Google operational state before DB-only cutover.

The script is streamed into the currently deployed (legacy) backend container.
It intentionally prints counts only: no client, order, or KIZ values leave the VDS.
"""

from __future__ import annotations

import json


RETURN_MARKERS = {"возврат", "returned", "return"}


def normalize(value):
    return str(value or "").strip()


def is_returned_record(record):
    status = normalize(record.get("return_status")).casefold()
    return status in RETURN_MARKERS or "возврат" in status


def order_is_returned(order):
    payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    return order.status == "returned" or normalize(payload.get("return_status")).casefold() == "returned"


def summarize(records_and_items, return_scan_ids):
    counters = {
        "records_total": 0,
        "active_records": 0,
        "returned_records": 0,
        "active_missing_backend": 0,
        "returned_missing_backend": 0,
        "active_codes_missing_backend": 0,
        "returned_codes_missing_backend": 0,
        "returned_orders_not_marked": 0,
        "returned_codes_without_return_movement": 0,
    }
    unmarked_order_ids = set()
    return_scan_ids = {str(value) for value in return_scan_ids if value is not None}

    for record, item in records_and_items:
        counters["records_total"] += 1
        archived = bool(record.get("archived"))
        returned = is_returned_record(record)
        if not archived:
            counters["active_records"] += 1
        if returned:
            counters["returned_records"] += 1

        if item is None:
            if not archived:
                counters["active_missing_backend"] += 1
            if returned:
                counters["returned_missing_backend"] += 1
            continue

        scans_by_code = {
            normalize(scan.code): scan
            for scan in (item.scan_codes or [])
            if normalize(scan.code)
        }
        for code in {normalize(value) for value in record.get("scanned_codes") or [] if normalize(value)}:
            scan = scans_by_code.get(code)
            if scan is None:
                if not archived:
                    counters["active_codes_missing_backend"] += 1
                if returned:
                    counters["returned_codes_missing_backend"] += 1
                continue
            if returned and str(scan.id) not in return_scan_ids:
                counters["returned_codes_without_return_movement"] += 1

        if returned and not order_is_returned(item.order):
            unmarked_order_ids.add(str(item.order.id))

    counters["returned_orders_not_marked"] = len(unmarked_order_ids)
    blocker_fields = (
        "active_missing_backend",
        "returned_missing_backend",
        "active_codes_missing_backend",
        "returned_codes_missing_backend",
        "returned_orders_not_marked",
        "returned_codes_without_return_movement",
    )
    counters["blockers"] = sum(counters[field] for field in blocker_fields)
    return {
        "schema_version": 1,
        "mode": "read_only_counts_only",
        "safe_to_cutover": counters["blockers"] == 0,
        **counters,
    }


def load_return_scan_ids(db, scan_ids, *, batch_size=1000):
    from sqlalchemy import select

    from app.models import KizMovement

    values = sorted({value for value in scan_ids if value is not None}, key=str)
    result = set()
    for start in range(0, len(values), batch_size):
        batch = values[start:start + batch_size]
        result.update(db.execute(
            select(KizMovement.scan_code_id)
            .where(KizMovement.scan_code_id.in_(batch))
            .where(KizMovement.movement_type == "return")
        ).scalars().all())
    return result


def run():
    from app.db import SessionLocal
    from app.google_sheets_sync_worker import (
        find_item_for_record,
        load_google_sheet_records,
        load_item_index,
    )

    records = load_google_sheet_records()
    with SessionLocal() as db:
        item_index = load_item_index(db, records)
        records_and_items = [
            (record, find_item_for_record(record, item_index))
            for record in records
        ]
        scan_ids = {
            scan.id
            for _record, item in records_and_items
            if item is not None
            for scan in (item.scan_codes or [])
        }
        result = summarize(records_and_items, load_return_scan_ids(db, scan_ids))
        db.rollback()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["safe_to_cutover"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "mode": "read_only_counts_only",
            "safe_to_cutover": False,
            "blockers": -1,
            "audit_error_type": type(exc).__name__,
        }, sort_keys=True))
        raise SystemExit(4)
