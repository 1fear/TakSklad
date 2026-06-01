import argparse
import json
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .google_sheets_exporter import format_skladbot_status, normalize_text, parse_int_value
from .google_sheets_sync_worker import load_google_sheet_records, open_data_sheet
from .models import Order, OrderItem
from .orders_service import COMPLETED_STATUSES, STATUS_REMOVED_FROM_GOOGLE


DEFAULT_BLOCK_PRICE = 240000


def item_source_key(item):
    raw_payload = getattr(item, "raw_payload", None) or {}
    source_import_id = normalize_text(raw_payload.get("source_import_id"))
    if source_import_id:
        return f"import:{source_import_id}"
    source_order_id = normalize_text(raw_payload.get("source_order_id"))
    if source_order_id:
        return f"order:{source_order_id}"
    return ""


def record_source_key(record):
    source_import_id = normalize_text(record.get("source_import_id"))
    if source_import_id:
        return f"import:{source_import_id}"
    source_order_id = normalize_text(record.get("source_order_id"))
    if source_order_id:
        return f"order:{source_order_id}"
    return ""


def item_label(item):
    order = item.order
    return {
        "order_id": str(getattr(order, "id", "")),
        "item_id": str(getattr(item, "id", "")),
        "source_key": item_source_key(item),
        "client": normalize_text(getattr(order, "client", "")),
        "product": normalize_text(getattr(item, "product", "")),
    }


def record_label(record):
    return {
        "row_number": record.get("row_number"),
        "source_key": record_source_key(record),
        "client": normalize_text(record.get("client")),
        "product": normalize_text(record.get("product")),
    }


def normalize_compare(value):
    return normalize_text(value).casefold()


def normalize_date_value(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return normalize_text(value)


def normalize_skladbot_status(value):
    return normalize_compare(format_skladbot_status(value))


def build_index(items, key_func):
    index = {}
    duplicates = []
    for item in items:
        key = key_func(item)
        if not key:
            continue
        if key in index:
            duplicates.append(key)
            continue
        index[key] = item
    return index, sorted(set(duplicates))


def active_backend_items(orders):
    items = []
    for order in orders:
        if normalize_text(getattr(order, "status", "")) in COMPLETED_STATUSES:
            continue
        items.extend(
            item
            for item in (getattr(order, "items", []) or [])
            if normalize_text(getattr(item, "status", "")) != STATUS_REMOVED_FROM_GOOGLE
        )
    return items


def compare_field(mismatches, field, backend_value, sheet_value, context, *, numeric=False, date_value=False):
    if numeric:
        backend_normalized = parse_int_value(backend_value)
        sheet_normalized = parse_int_value(sheet_value)
    elif date_value:
        backend_normalized = normalize_date_value(backend_value)
        sheet_normalized = normalize_date_value(sheet_value)
    else:
        backend_normalized = normalize_compare(backend_value)
        sheet_normalized = normalize_compare(sheet_value)

    if backend_normalized != sheet_normalized:
        mismatches.append({
            **context,
            "field": field,
            "backend": backend_value,
            "google_sheet": sheet_value,
        })


def compare_matched_item(record, item):
    order = item.order
    order_raw = getattr(order, "raw_payload", None) or {}
    item_raw = getattr(item, "raw_payload", None) or {}
    context = {
        "row_number": record.get("row_number"),
        "source_key": record_source_key(record),
        "order_id": str(getattr(order, "id", "")),
        "item_id": str(getattr(item, "id", "")),
    }
    mismatches = []
    compare_field(mismatches, "order_date", getattr(order, "order_date", None), record.get("order_date"), context, date_value=True)
    compare_field(mismatches, "payment_type", getattr(order, "payment_type", ""), record.get("payment_type"), context)
    compare_field(mismatches, "client", getattr(order, "client", ""), record.get("client"), context)
    compare_field(mismatches, "address", getattr(order, "address", ""), record.get("address"), context)
    compare_field(mismatches, "representative", getattr(order, "representative", ""), record.get("representative"), context)
    compare_field(mismatches, "product", getattr(item, "product", ""), record.get("product"), context)
    compare_field(mismatches, "quantity_blocks", getattr(item, "quantity_blocks", 0), record.get("quantity_blocks"), context, numeric=True)
    compare_field(mismatches, "quantity_pieces", getattr(item, "quantity_pieces", 0), record.get("quantity_pieces"), context, numeric=True)

    sheet_codes = record.get("scanned_codes") or []
    if sheet_codes:
        compare_field(mismatches, "scanned_blocks", getattr(item, "scanned_blocks", 0), len(sheet_codes), context, numeric=True)

    compare_field(
        mismatches,
        "skladbot_request_number",
        order_raw.get("skladbot_request_number"),
        record.get("skladbot_request_number"),
        context,
    )
    compare_field(
        mismatches,
        "skladbot_request_id",
        order_raw.get("skladbot_request_id"),
        record.get("skladbot_request_id"),
        context,
    )
    if normalize_text(order_raw.get("skladbot_status")) or normalize_text(record.get("skladbot_status")):
        backend_status = normalize_skladbot_status(order_raw.get("skladbot_status"))
        sheet_status = normalize_skladbot_status(record.get("skladbot_status"))
        if backend_status != sheet_status:
            mismatches.append({
                **context,
                "field": "skladbot_status",
                "backend": format_skladbot_status(order_raw.get("skladbot_status")),
                "google_sheet": format_skladbot_status(record.get("skladbot_status")),
            })

    block_price = parse_int_value(item_raw.get("block_price")) or DEFAULT_BLOCK_PRICE
    expected_line_total = parse_int_value(getattr(item, "quantity_blocks", 0)) * block_price
    line_total = parse_int_value(item_raw.get("line_total"))
    calculated_line_total = parse_int_value(item_raw.get("calculated_line_total"))
    if expected_line_total > 0 and line_total and line_total != expected_line_total:
        mismatches.append({
            **context,
            "field": "line_total",
            "backend": line_total,
            "expected": expected_line_total,
        })
    if expected_line_total > 0 and calculated_line_total and calculated_line_total != expected_line_total:
        mismatches.append({
            **context,
            "field": "calculated_line_total",
            "backend": calculated_line_total,
            "expected": expected_line_total,
        })
    return mismatches


def verify_google_backend_sync(records, orders, detail_limit=20):
    detail_limit = max(1, min(int(detail_limit or 20), 100))
    backend_items = active_backend_items(orders)
    sheet_index, duplicate_sheet_keys = build_index(records, record_source_key)
    backend_index, duplicate_backend_keys = build_index(backend_items, item_source_key)

    sheet_missing_backend = []
    backend_missing_sheet = []
    field_mismatches = []

    for key, record in sheet_index.items():
        item = backend_index.get(key)
        if item is None:
            sheet_missing_backend.append(record_label(record))
            continue
        field_mismatches.extend(compare_matched_item(record, item))

    for key, item in backend_index.items():
        if key not in sheet_index:
            backend_missing_sheet.append(item_label(item))

    errors = []
    if duplicate_sheet_keys:
        errors.append(f"duplicate Google Sheets source keys: {len(duplicate_sheet_keys)}")
    if duplicate_backend_keys:
        errors.append(f"duplicate backend source keys: {len(duplicate_backend_keys)}")
    if sheet_missing_backend:
        errors.append(f"Google Sheets rows missing in backend: {len(sheet_missing_backend)}")
    if backend_missing_sheet:
        errors.append(f"backend active items missing in Google Sheets data: {len(backend_missing_sheet)}")
    if field_mismatches:
        errors.append(f"field mismatches: {len(field_mismatches)}")

    return {
        "status": "failed" if errors else "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
        "google_rows": len(records),
        "backend_active_orders": len([
            order
            for order in orders
            if normalize_text(getattr(order, "status", "")) not in COMPLETED_STATUSES
            and any(
                normalize_text(getattr(item, "status", "")) != STATUS_REMOVED_FROM_GOOGLE
                for item in (getattr(order, "items", []) or [])
            )
        ]),
        "backend_active_items": len(backend_items),
        "matched_items": len(set(sheet_index) & set(backend_index)),
        "duplicate_sheet_keys": duplicate_sheet_keys[:detail_limit],
        "duplicate_backend_keys": duplicate_backend_keys[:detail_limit],
        "sheet_missing_backend": sheet_missing_backend[:detail_limit],
        "backend_missing_sheet": backend_missing_sheet[:detail_limit],
        "field_mismatches": field_mismatches[:detail_limit],
        "field_mismatch_count": len(field_mismatches),
        "sheet_missing_backend_count": len(sheet_missing_backend),
        "backend_missing_sheet_count": len(backend_missing_sheet),
    }


def diagnose_google_backend_sync(detail_limit=20):
    sheet = open_data_sheet()
    records = load_google_sheet_records(sheet=sheet)
    with SessionLocal() as db:
        orders = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(~Order.status.in_(COMPLETED_STATUSES))
            .order_by(Order.order_date.asc(), Order.client.asc())
        ).scalars().all()
        return verify_google_backend_sync(records, orders, detail_limit=detail_limit)


def main():
    parser = argparse.ArgumentParser(description="Read-only Google Sheets data vs backend sync diagnostic.")
    parser.add_argument("--detail-limit", type=int, default=20, help="Max mismatch details to include.")
    args = parser.parse_args()
    print(json.dumps(diagnose_google_backend_sync(detail_limit=args.detail_limit), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
