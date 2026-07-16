import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

try:
    from backend.app.db import SessionLocal
    from backend.app.excel_importer import excel_file_to_import_payload
    from backend.app.imports_service import normalize_import_row
    from backend.app.models import AuditLog, Order, OrderItem
    from backend.app.orders_service import STATUS_COMPLETED
except ModuleNotFoundError:
    from app.db import SessionLocal
    from app.excel_importer import excel_file_to_import_payload
    from app.imports_service import normalize_import_row
    from app.models import AuditLog, Order, OrderItem
    from app.orders_service import STATUS_COMPLETED


APPLY_CONFIRM_TOKEN = "APPLY_CHAPMAN_03062026"


def main():
    args = parse_args()
    expected = load_expected_rows(args.files, args.shipment_date)
    with SessionLocal() as db:
        report = build_report(db, expected)
        if args.apply:
            if args.confirm != APPLY_CONFIRM_TOKEN:
                raise SystemExit(f"--confirm must be {APPLY_CONFIRM_TOKEN}")
            apply_result = apply_repair(db, expected, report, complete_without_kiz=args.complete_without_kiz)
            report["apply"] = apply_result
            report = build_report(db, expected, previous_report=report)

    output = write_report(report, args.output)
    print(json.dumps({
        "status": report["status"],
        "expected_rows": report["summary"]["expected_rows"],
        "matched_backend": report["summary"]["matched_backend"],
        "missing_backend": report["summary"]["missing_backend"],
        "field_mismatches": report["summary"]["field_mismatches"],
        "scanned_conflicts": report["summary"]["scanned_conflicts"],
        "unsafe_to_apply": report["summary"]["unsafe_to_apply"],
        "report": str(output),
    }, ensure_ascii=False, indent=2))


def parse_args():
    parser = argparse.ArgumentParser(description="Dry-run/repair Chapman Excel rows against TakSklad Postgres.")
    parser.add_argument("files", nargs="+", help="Original Chapman Excel files.")
    parser.add_argument("--shipment-date", default="03.06.2026", help="Force shipment date for Excel rows.")
    parser.add_argument("--apply", action="store_true", help="Apply safe DB repair. Requires --confirm.")
    parser.add_argument("--confirm", default="", help=f"Confirmation token: {APPLY_CONFIRM_TOKEN}")
    parser.add_argument("--complete-without-kiz", action="store_true", help="Mark safe repaired orders completed in PostgreSQL.")
    parser.add_argument("--output", default="", help="JSON report path.")
    return parser.parse_args()


def load_expected_rows(files, shipment_date):
    expected = {}
    duplicates = []
    sources = []
    for file_path in files:
        payload = excel_file_to_import_payload(file_path, shipment_date=shipment_date, source="repair")
        sources.append({
            "filename": payload["filename"],
            "sha256": payload["sha256"],
            "rows": len(payload["rows"]),
            "meta": payload.get("meta") or {},
        })
        for raw_row in payload["rows"]:
            row = normalize_import_row(raw_row)
            source_import_id = row["source_import_id"]
            if not source_import_id:
                source_import_id = row["item_key"]
            row["_raw_row"] = raw_row
            row["_filename"] = payload["filename"]
            row["_sha256"] = payload["sha256"]
            if source_import_id in expected:
                duplicates.append(source_import_id)
            expected[source_import_id] = row
    return {"rows": expected, "duplicates": sorted(set(duplicates)), "sources": sources}


def build_report(db, expected, previous_report=None):
    expected_rows = expected["rows"]
    expected_ids = set(expected_rows)
    items = db.execute(
        select(OrderItem)
        .options(
            selectinload(OrderItem.order).selectinload(Order.items),
            selectinload(OrderItem.scan_codes),
        )
    ).scalars().all()
    backend_by_import_id = {}
    backend_duplicates = []
    for item in items:
        source_import_id = str((item.raw_payload or {}).get("source_import_id") or "").strip()
        if not source_import_id:
            continue
        if source_import_id in backend_by_import_id:
            backend_duplicates.append(source_import_id)
            continue
        backend_by_import_id[source_import_id] = item

    missing_backend = []
    extra_backend = []
    field_mismatches = []
    scanned_conflicts = []
    matched = 0

    for source_import_id, row in expected_rows.items():
        item = backend_by_import_id.get(source_import_id)
        if item is None:
            missing_backend.append(row_label(source_import_id, row))
            continue
        matched += 1
        field_mismatches.extend(compare_item(source_import_id, row, item))
        if int(item.scanned_blocks or 0) > 0 or len(item.scan_codes or []) > 0:
            scanned_conflicts.append({
                **item_label(source_import_id, item),
                "scanned_blocks": int(item.scanned_blocks or 0),
                "scan_codes": len(item.scan_codes or []),
            })

    for source_import_id, item in backend_by_import_id.items():
        if source_import_id in expected_ids:
            continue
        source_file = str((item.raw_payload or {}).get("source_file") or "")
        if source_file in {source["filename"] for source in expected["sources"]}:
            extra_backend.append(item_label(source_import_id, item))

    unsafe_to_apply = bool(expected["duplicates"] or backend_duplicates or missing_backend or scanned_conflicts)
    status = "blocked" if unsafe_to_apply else "ok"
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "sources": expected["sources"],
        "summary": {
            "expected_rows": len(expected_rows),
            "matched_backend": matched,
            "missing_backend": len(missing_backend),
            "extra_backend_from_same_files": len(extra_backend),
            "field_mismatches": len(field_mismatches),
            "scanned_conflicts": len(scanned_conflicts),
            "expected_duplicate_source_import_ids": len(expected["duplicates"]),
            "backend_duplicate_source_import_ids": len(set(backend_duplicates)),
            "unsafe_to_apply": unsafe_to_apply,
        },
        "expected_duplicate_source_import_ids": expected["duplicates"][:200],
        "backend_duplicate_source_import_ids": sorted(set(backend_duplicates))[:200],
        "missing_backend": missing_backend[:300],
        "extra_backend_from_same_files": extra_backend[:300],
        "field_mismatches": field_mismatches[:500],
        "scanned_conflicts": scanned_conflicts[:300],
    }
    if previous_report and previous_report.get("apply"):
        report["apply"] = previous_report["apply"]
    return report


def apply_repair(db, expected, report, complete_without_kiz=False):
    if report["summary"]["unsafe_to_apply"]:
        raise SystemExit("Refusing apply: dry-run report has unsafe rows")

    expected_rows = expected["rows"]
    items = db.execute(
        select(OrderItem)
        .options(
            selectinload(OrderItem.order).selectinload(Order.items),
            selectinload(OrderItem.scan_codes),
        )
    ).scalars().all()
    backend_by_import_id = {
        str((item.raw_payload or {}).get("source_import_id") or "").strip(): item
        for item in items
        if str((item.raw_payload or {}).get("source_import_id") or "").strip() in expected_rows
    }
    target_order_ids = {str(item.order.id) for item in backend_by_import_id.values()}
    non_target_items = []
    for item in backend_by_import_id.values():
        order = item.order
        for order_item in order.items:
            source_import_id = str((order_item.raw_payload or {}).get("source_import_id") or "").strip()
            if source_import_id not in expected_rows:
                non_target_items.append(item_label(source_import_id, order_item))
    if non_target_items:
        raise SystemExit("Refusing apply: target orders contain non-target items")

    repaired_items = 0
    repaired_orders = set()
    now = datetime.now(timezone.utc)
    for source_import_id, row in expected_rows.items():
        item = backend_by_import_id[source_import_id]
        order = item.order
        order.order_date = row["order_date"]
        order.payment_type = row["payment_type"]
        order.client = row["client"]
        order.address = row["address"]
        order.representative = row["representative"]
        order_raw = dict(order.raw_payload or {})
        order_raw.update({
            "coordinates": row["coordinates"],
            "repair_source": "chapman_excel_reconcile",
            "repair_at": now.isoformat(),
        })
        if row["skladbot_request_number"]:
            order_raw["skladbot_request_number"] = row["skladbot_request_number"]
        if row["skladbot_request_id"]:
            order_raw["skladbot_request_id"] = row["skladbot_request_id"]
        order.raw_payload = order_raw

        item.product = row["product"]
        item.quantity_pieces = row["quantity_pieces"]
        item.quantity_blocks = row["quantity_blocks"]
        item.pieces_per_block = row["pieces_per_block"]
        item_raw = dict(item.raw_payload or {})
        item_raw.update({
            "item_key": row["item_key"],
            "business_line_key": row["item_key"],
            "source_order_id": row["source_order_id"],
            "source_import_id": row["source_import_id"],
            "source_file": row["source_file"],
            "source_row": row["source_row"],
            "block_price": row["block_price"],
            "imported_unit_price": row["imported_unit_price"],
            "imported_line_total": row["imported_line_total"],
            "line_total": row["line_total"],
            "calculated_line_total": row["calculated_line_total"],
            "raw_row": row["_raw_row"],
            "repair_source": "chapman_excel_reconcile",
            "repair_at": now.isoformat(),
        })
        item.raw_payload = item_raw
        repaired_items += 1
        repaired_orders.add(str(order.id))

    if complete_without_kiz:
        for item in backend_by_import_id.values():
            item.status = STATUS_COMPLETED
            item.order.status = STATUS_COMPLETED
        for order_id in sorted(target_order_ids):
            db.add(AuditLog(
                action="order_completed_without_kiz_repair",
                entity_type="order",
                entity_id=order_id,
                payload={"repair_source": "chapman_excel_reconcile"},
            ))

    db.add(AuditLog(
        action="chapman_excel_reconcile_applied",
        entity_type="order_batch",
        entity_id=",".join(sorted(repaired_orders))[:120],
        payload={
            "repaired_items": repaired_items,
            "repaired_orders": len(repaired_orders),
            "complete_without_kiz": complete_without_kiz,
            "queued_archive_exports": 0,
        },
    ))
    db.commit()
    return {
        "repaired_items": repaired_items,
        "repaired_orders": len(repaired_orders),
        "complete_without_kiz": complete_without_kiz,
        "queued_archive_exports": 0,
    }


def compare_item(source_import_id, row, item):
    order = item.order
    order_raw = order.raw_payload or {}
    item_raw = item.raw_payload or {}
    checks = [
        ("order_date", normalize_date(order.order_date), normalize_date(row["order_date"])),
        ("payment_type", normalize_text(order.payment_type), normalize_text(row["payment_type"])),
        ("client", normalize_text(order.client), normalize_text(row["client"])),
        ("address", normalize_text(order.address), normalize_text(row["address"])),
        ("coordinates", normalize_text(order_raw.get("coordinates")), normalize_text(row["coordinates"])),
        ("representative", normalize_text(order.representative), normalize_text(row["representative"])),
        ("product", normalize_text(item.product), normalize_text(row["product"])),
        ("quantity_pieces", int(item.quantity_pieces or 0), int(row["quantity_pieces"] or 0)),
        ("quantity_blocks", int(item.quantity_blocks or 0), int(row["quantity_blocks"] or 0)),
        ("pieces_per_block", int(item.pieces_per_block or 0), int(row["pieces_per_block"] or 0)),
        ("block_price", parse_int(item_raw.get("block_price")), int(row["block_price"] or 0)),
        ("line_total", parse_int(item_raw.get("line_total")), int(row["line_total"] or 0)),
    ]
    mismatches = []
    for field, backend_value, expected_value in checks:
        if normalize_compare(backend_value) != normalize_compare(expected_value):
            mismatches.append({
                **item_label(source_import_id, item),
                "field": field,
                "backend": backend_value,
                "expected": expected_value,
            })
    return mismatches


def row_label(source_import_id, row):
    return {
        "source_import_id": source_import_id,
        "source_file": row.get("source_file") or row.get("_filename") or "",
        "source_row": row.get("source_row") or "",
        "client": row.get("client") or "",
        "product": row.get("product") or "",
        "quantity_blocks": row.get("quantity_blocks") or 0,
    }


def item_label(source_import_id, item):
    return {
        "source_import_id": source_import_id,
        "order_id": str(item.order.id),
        "item_id": str(item.id),
        "source_file": (item.raw_payload or {}).get("source_file") or "",
        "source_row": (item.raw_payload or {}).get("source_row") or "",
        "client": item.order.client,
        "product": item.product,
    }


def write_report(report, output):
    if output:
        output_path = Path(output)
    else:
        output_dir = Path("outputs") / "reconcile"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"chapman_reconcile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output_path


def normalize_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return normalize_text(value)


def normalize_text(value):
    return str(value or "").strip()


def normalize_compare(value):
    return normalize_text(value).casefold()


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    main()
