import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import SessionLocal
from .google_sheets_exporter import (
    SHEET_NAME,
    SPREADSHEET_ID,
    archive_backend_order_to_google_sheets,
    ensure_import_sheet_layout,
    get_cell,
    get_google_client,
    get_header_index,
    normalize_header_name,
    normalize_text,
    parse_int_value,
)
from .kiz_movements_service import (
    find_other_item_scan,
    find_same_item_scan,
    kiz_is_available_for_outbound,
    latest_kiz_movement,
    outbound_movement_type_for,
    record_kiz_movement,
)
from .models import AuditLog, Order, OrderItem, ScanCode
from .orders_service import (
    COMPLETED_STATUSES,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    STATUS_REMOVED_FROM_GOOGLE,
    STATUS_RETURNED,
)
from .google_sheets_pending import process_pending_google_sheets_exports


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


ORDER_DATE_COLUMN = "Дата отгрузки"
LEGACY_ORDER_DATE_COLUMN = "Дата получения заказа"
STATUS_COLUMN = "Статус"
DEFAULT_BLOCK_PRICE = 240000
ARCHIVE_SHEET_NAME = "Архив"
RETURN_STATUS_COLUMN = "Статус возврата"
RETURN_DATE_COLUMN = "Дата возврата"
RETURN_REFERENCE_COLUMN = "Основание возврата"
RETURNED_BY_COLUMN = "Принял возврат"
RETURN_STATUS_VALUE = "Возврат"


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_bool(name, default=False):
    text = normalize_text(os.environ.get(name)).casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "да"}


def sync_google_sheet_to_backend(db: Session, sheet=None, now=None, archive_completed_data_rows=None):
    now = now or datetime.now(timezone.utc)
    if archive_completed_data_rows is None:
        archive_completed_data_rows = sheet is None
    records = load_google_sheet_records(sheet)
    if not records:
        return build_result(rows=0)

    item_index = load_item_index(db)
    result = build_result(rows=len(records))
    completed_order_ids_to_archive = set()
    matched_item_ids = set()
    for record in records:
        item = find_item_for_record(record, item_index)
        if item is None:
            result["missing"] += 1
            continue

        matched_item_ids.add(item.id)
        item_result = apply_record_to_item(db, item, record, now)
        result["matched"] += 1
        result["orders_updated"] += 1 if item_result["order_changed"] else 0
        result["items_updated"] += 1 if item_result["item_changed"] else 0
        result["conflicts"] += len(item_result["conflicts"])
        if (
            archive_completed_data_rows
            and record.get("source_sheet") == SHEET_NAME
            and item.order.status in COMPLETED_STATUSES
        ):
            completed_order_ids_to_archive.add(item.order_id)

    db.add(AuditLog(
        action="google_sheets_backend_sync",
        entity_type="google_sheets",
        entity_id=SHEET_NAME,
        payload=result,
    ))
    mark_backend_items_missing_from_google(db, item_index, matched_item_ids, result, now)
    db.commit()
    archive_completed_orders_from_data_sheet(db, completed_order_ids_to_archive, result)
    return result


def build_result(rows=0):
    return {
        "rows": rows,
        "matched": 0,
        "missing": 0,
        "orders_updated": 0,
        "items_updated": 0,
        "conflicts": 0,
        "archived": 0,
        "removed": 0,
    }


def open_data_sheet():
    client = get_google_client()
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def load_google_sheet_records(sheet=None):
    if sheet is not None:
        ensure_import_sheet_layout(sheet)
        return parse_sheet_records(sheet.get_all_values(), source_sheet=SHEET_NAME)

    client = get_google_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    records = []
    data_sheet = spreadsheet.worksheet(SHEET_NAME)
    ensure_import_sheet_layout(data_sheet)
    records.extend(parse_sheet_records(data_sheet.get_all_values(), source_sheet=SHEET_NAME))

    try:
        archive_sheet = spreadsheet.worksheet(ARCHIVE_SHEET_NAME)
    except Exception:
        archive_sheet = None
    if archive_sheet is not None:
        ensure_import_sheet_layout(archive_sheet)
        records.extend(parse_sheet_records(
            archive_sheet.get_all_values(),
            source_sheet=ARCHIVE_SHEET_NAME,
            archived=True,
        ))
    return records


def parse_sheet_records(all_rows, source_sheet=SHEET_NAME, archived=False):
    if not all_rows:
        return []

    header = [normalize_header_name(value) for value in all_rows[0]]
    header_idx = get_header_index(header)
    records = []
    for row_number, row in enumerate(all_rows[1:], start=2):
        import_id = get_cell(row, header_idx.get("ID импорта"))
        order_id = get_cell(row, header_idx.get("ID заказа"))
        if not import_id and not order_id:
            continue
        product = get_cell(row, header_idx.get("Товары"))
        quantity_blocks = parse_int_value(get_cell(row, header_idx.get("Кол-во блок")))
        quantity_pieces = parse_int_value(get_cell(row, header_idx.get("Кол-во ШТ")))
        if not product and quantity_blocks <= 0 and quantity_pieces <= 0:
            continue
        records.append({
            "row_number": row_number,
            "source_sheet": source_sheet,
            "archived": archived,
            "source_import_id": import_id,
            "source_order_id": order_id,
            "order_date": parse_sheet_date(
                get_cell(row, header_idx.get(ORDER_DATE_COLUMN))
                or get_cell(row, header_idx.get(LEGACY_ORDER_DATE_COLUMN))
            ),
            "payment_type": get_cell(row, header_idx.get("Тип оплаты")),
            "client": get_cell(row, header_idx.get("Клиент")),
            "address": get_cell(row, header_idx.get("Адрес")),
            "representative": get_cell(row, header_idx.get("Торговый представитель")),
            "product": product,
            "quantity_pieces": quantity_pieces,
            "quantity_blocks": quantity_blocks,
            "block_price": parse_int_value(get_cell(row, header_idx.get("Цена за блок"))),
            "line_total": parse_int_value(get_cell(row, header_idx.get("Сумма позиции"))),
            "scanned_codes": split_codes(get_cell(row, header_idx.get("Отсканированные коды"))),
            "status": get_cell(row, header_idx.get(STATUS_COLUMN)),
            "source_file": get_cell(row, header_idx.get("Источник файла")),
            "source_row": get_cell(row, header_idx.get("Строка файла")),
            "skladbot_request_number": get_cell(row, header_idx.get("Номер заявки SkladBot")),
            "skladbot_request_id": get_cell(row, header_idx.get("ID заявки SkladBot")),
            "skladbot_status": get_cell(row, header_idx.get("Статус SkladBot")),
            "return_status": get_cell(row, header_idx.get(RETURN_STATUS_COLUMN)),
            "returned_at": get_cell(row, header_idx.get(RETURN_DATE_COLUMN)),
            "return_reference": get_cell(row, header_idx.get(RETURN_REFERENCE_COLUMN)),
            "returned_by": get_cell(row, header_idx.get(RETURNED_BY_COLUMN)),
        })
    return records


def parse_sheet_date(value):
    text = normalize_text(value)
    if not text:
        return None
    if " " in text:
        text = text.split(" ", 1)[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def load_item_index(db: Session):
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
    ).scalars().all()
    index = {"source_import_id": {}, "source_order_id": {}}
    for order in orders:
        for item in order.items:
            raw_payload = item.raw_payload or {}
            source_import_id = normalize_text(raw_payload.get("source_import_id"))
            source_order_id = normalize_text(raw_payload.get("source_order_id"))
            if source_import_id:
                index["source_import_id"].setdefault(source_import_id, item)
            if source_order_id:
                index["source_order_id"].setdefault(source_order_id, item)
    return index


def find_item_for_record(record, item_index):
    source_import_id = normalize_text(record.get("source_import_id"))
    if source_import_id:
        item = item_index["source_import_id"].get(source_import_id)
        if item is not None:
            return item
    source_order_id = normalize_text(record.get("source_order_id"))
    if source_order_id:
        return item_index["source_order_id"].get(source_order_id)
    return None


def apply_record_to_item(db: Session, item: OrderItem, record, now):
    order = item.order
    conflicts = []
    return_fields_allowed = google_return_fields_allowed(order, record)
    if is_returned_record(record) and not return_fields_allowed:
        conflicts.append({
            "field": "return_status",
            "old": (order.raw_payload or {}).get("return_status") or "",
            "new": normalize_text(record.get("return_status")),
            "reason": "Google Sheets return marker ignored; backend return endpoint is source of truth",
        })

    order_changed = update_order_fields(order, record, apply_returns=return_fields_allowed)
    item_changed = update_item_fields(item, record, conflicts)
    scans_changed = update_item_scans_from_record(db, item, record, conflicts, now)
    status_changed = update_status_from_record(order, item, record, allow_return=return_fields_allowed)

    if order_changed or status_changed:
        raw_payload = dict(order.raw_payload or {})
        apply_skladbot_fields(raw_payload, record)
        raw_payload["google_sheet_synced_at"] = now.isoformat()
        raw_payload["google_sheet_row_number"] = record["row_number"]
        raw_payload["google_sheet_source_sheet"] = record.get("source_sheet") or SHEET_NAME
        order.raw_payload = raw_payload

    if item_changed or scans_changed or status_changed:
        raw_payload = dict(item.raw_payload or {})
        raw_payload["google_sheet_synced_at"] = now.isoformat()
        raw_payload["google_sheet_row_number"] = record["row_number"]
        raw_payload["google_sheet_source_sheet"] = record.get("source_sheet") or SHEET_NAME
        if record.get("source_file"):
            raw_payload["source_file"] = record["source_file"]
        if record.get("source_row"):
            raw_payload["source_row"] = record["source_row"]
        update_item_money_payload(raw_payload, record, quantity_blocks=item.quantity_blocks)
        item.raw_payload = raw_payload

    if conflicts:
        db.add(AuditLog(
            action="google_sheets_backend_sync_conflict",
            entity_type="order_item",
            entity_id=str(item.id),
            payload={
                "order_id": str(order.id),
                "row_number": record["row_number"],
                "source_import_id": record.get("source_import_id"),
                "conflicts": conflicts,
            },
        ))

    return {
        "order_changed": order_changed,
        "item_changed": item_changed or scans_changed or status_changed,
        "conflicts": conflicts,
    }


def update_order_fields(order: Order, record, apply_returns=True):
    changed = False
    for field_name, value in (
        ("order_date", record.get("order_date")),
        ("payment_type", record.get("payment_type")),
        ("client", record.get("client")),
        ("address", record.get("address")),
        ("representative", record.get("representative")),
    ):
        if value in (None, ""):
            continue
        if getattr(order, field_name) != value:
            setattr(order, field_name, value)
            changed = True

    raw_payload = dict(order.raw_payload or {})
    before = dict(raw_payload)
    apply_skladbot_fields(raw_payload, record)
    if apply_returns:
        apply_return_fields(raw_payload, record)
    if raw_payload != before:
        order.raw_payload = raw_payload
        changed = True
    return changed


def apply_skladbot_fields(raw_payload, record):
    field_map = {
        "skladbot_request_number": "skladbot_request_number",
        "skladbot_request_id": "skladbot_request_id",
        "skladbot_status": "skladbot_status",
    }
    for record_field, payload_field in field_map.items():
        value = normalize_text(record.get(record_field))
        if value:
            raw_payload[payload_field] = value


def apply_return_fields(raw_payload, record):
    if not is_returned_record(record):
        return
    raw_payload["return_status"] = "returned"
    if normalize_text(record.get("returned_at")):
        raw_payload["returned_at"] = normalize_text(record.get("returned_at"))
    if normalize_text(record.get("return_reference")):
        raw_payload["return_reference"] = normalize_text(record.get("return_reference"))
    if normalize_text(record.get("returned_by")):
        raw_payload["returned_by"] = normalize_text(record.get("returned_by"))


def google_return_fields_allowed(order: Order, record):
    if not is_returned_record(record):
        return True
    raw_payload = order.raw_payload or {}
    return order.status == STATUS_RETURNED or raw_payload.get("return_status") == "returned"


def update_item_fields(item: OrderItem, record, conflicts):
    changed = False
    new_product = normalize_text(record.get("product"))
    if new_product and item.product != new_product:
        if item.scanned_blocks > 0:
            conflicts.append({
                "field": "product",
                "old": item.product,
                "new": new_product,
                "reason": "item already has scanned codes",
            })
        else:
            item.product = new_product
            changed = True

    new_blocks = int(record.get("quantity_blocks") or 0)
    if new_blocks > 0 and item.quantity_blocks != new_blocks:
        if new_blocks < item.scanned_blocks:
            conflicts.append({
                "field": "quantity_blocks",
                "old": item.quantity_blocks,
                "new": new_blocks,
                "scanned_blocks": item.scanned_blocks,
                "reason": "new quantity is less than already scanned blocks",
            })
        else:
            item.quantity_blocks = new_blocks
            changed = True

    new_pieces = int(record.get("quantity_pieces") or 0)
    if new_pieces > 0 and item.quantity_pieces != new_pieces:
        item.quantity_pieces = new_pieces
        changed = True

    if conflicts:
        return changed

    raw_payload = dict(item.raw_payload or {})
    before = dict(raw_payload)
    update_item_money_payload(raw_payload, record, quantity_blocks=item.quantity_blocks)
    if raw_payload != before:
        item.raw_payload = raw_payload
        changed = True
    return changed


def update_item_money_payload(raw_payload, record, quantity_blocks=0):
    block_price = (
        int(record.get("block_price") or 0)
        or parse_int_value(raw_payload.get("block_price"))
        or DEFAULT_BLOCK_PRICE
    )
    if block_price > 0:
        raw_payload["block_price"] = block_price

    sheet_line_total = int(record.get("line_total") or 0)
    blocks = int(quantity_blocks or record.get("quantity_blocks") or 0)
    calculated_line_total = blocks * block_price if blocks > 0 and block_price > 0 else 0
    if calculated_line_total > 0:
        raw_payload["calculated_line_total"] = calculated_line_total
        raw_payload["line_total"] = calculated_line_total
        if sheet_line_total > 0 and sheet_line_total != calculated_line_total:
            raw_payload["google_sheet_line_total"] = sheet_line_total
    elif sheet_line_total > 0:
        raw_payload["line_total"] = sheet_line_total


def split_codes(value):
    return [
        item.strip(" \t\r\n")
        for item in str(value or "").replace("\r", "\n").split("\n")
        if item.strip(" \t\r\n")
    ]


def update_item_scans_from_record(db: Session, item: OrderItem, record, conflicts, now):
    scanned_codes = list(dict.fromkeys(record.get("scanned_codes") or []))
    if not scanned_codes:
        return False

    existing_for_item = {scan.code for scan in item.scan_codes}
    changed = False
    for code in scanned_codes:
        if code in existing_for_item:
            continue
        same_item_scan = find_same_item_scan(db, code=code, order_item_id=item.id)
        if same_item_scan is not None:
            existing_for_item.add(code)
            continue
        other_item_scan = find_other_item_scan(db, code=code, order_item_id=item.id)
        latest_movement = latest_kiz_movement(db, code)
        if other_item_scan is not None and latest_movement is None:
            conflicts.append({
                "field": "scanned_codes",
                "code": code,
                "reason": "code already exists for another order item",
            })
            continue
        if other_item_scan is not None and not kiz_is_available_for_outbound(latest_movement):
            conflicts.append({
                "field": "scanned_codes",
                "code": code,
                "reason": "code already exists for another order item",
            })
            continue
        if other_item_scan is None and latest_movement is not None and not kiz_is_available_for_outbound(latest_movement):
            conflicts.append({
                "field": "scanned_codes",
                "code": code,
                "reason": "code already exists for another order item",
            })
            continue
        scan = ScanCode(
            order_item_id=item.id,
            code=code,
            source="google_sheets",
            scanned_at=now,
            raw_payload={
                "google_sheet_row_number": record.get("row_number"),
                "google_sheet_source_sheet": record.get("source_sheet") or SHEET_NAME,
            },
        )
        item.scan_codes.append(scan)
        db.flush()
        record_kiz_movement(
            db,
            code=code,
            movement_type=outbound_movement_type_for(latest_movement),
            order_id=item.order_id,
            order_item_id=item.id,
            scan_code_id=scan.id,
            source="google_sheets",
            occurred_at=now,
            raw_payload={
                "google_sheet_row_number": record.get("row_number"),
                "google_sheet_source_sheet": record.get("source_sheet") or SHEET_NAME,
                "previous_movement_type": latest_movement.movement_type if latest_movement else "",
            },
        )
        existing_for_item.add(code)
        changed = True

    if conflicts:
        return changed

    new_scanned_blocks = max(item.scanned_blocks, len(scanned_codes))
    if item.scanned_blocks != new_scanned_blocks:
        item.scanned_blocks = new_scanned_blocks
        changed = True
    return changed


def update_status_from_record(order: Order, item: OrderItem, record, allow_return=True):
    status = normalize_text(record.get("status")).casefold()
    returned = bool(allow_return and is_returned_record(record))
    requires_full_scan = bool(item.requires_kiz and item.quantity_blocks > 0)
    fully_scanned = not requires_full_scan or item.scanned_blocks >= item.quantity_blocks
    google_requests_completion = (
        record.get("archived")
        or status in {"выполнено", "completed", "done", "closed", "готово"}
    )
    should_complete = (
        returned
        or (google_requests_completion and fully_scanned)
        or (requires_full_scan and fully_scanned)
    )
    if not should_complete:
        return False

    changed = False
    if item.status not in COMPLETED_STATUSES:
        item.status = "completed"
        changed = True

    if returned:
        if order.status != STATUS_RETURNED:
            order.status = STATUS_RETURNED
            changed = True
        return changed

    if all(order_item.status in COMPLETED_STATUSES for order_item in order.items):
        if order.status not in COMPLETED_STATUSES:
            order.status = "completed"
            changed = True
    return changed


def archive_completed_orders_from_data_sheet(db: Session, order_ids, result):
    order_ids = list(dict.fromkeys(order_ids or []))
    if not order_ids:
        return

    for order_id in order_ids:
        order = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(Order.id == order_id)
        ).scalar_one_or_none()
        if order is None or order.status not in COMPLETED_STATUSES:
            continue
        try:
            export_result = archive_backend_order_to_google_sheets(order)
        except Exception as exc:
            logging.exception("Google Sheets sync: failed to archive completed order %s", order_id)
            export_result = {"status": "error", "updated": 0, "error": str(exc)}

        result["archived"] += int(export_result.get("updated") or 0)
        db.add(AuditLog(
            action="google_sheets_sync_archive_export",
            entity_type="order",
            entity_id=str(order.id),
            payload=export_result,
        ))
    db.commit()


def mark_backend_items_missing_from_google(db: Session, item_index, matched_item_ids, result, now):
    seen = set()
    for source_index in item_index.values():
        for item in source_index.values():
            if item.id in seen or item.id in matched_item_ids:
                continue
            seen.add(item.id)
            if item.status in COMPLETED_STATUSES or item.status in (
                STATUS_ARCHIVED_NO_KIZ,
                STATUS_CANCELLED,
                STATUS_REMOVED_FROM_GOOGLE,
            ):
                continue
            result["conflicts"] += 1
            db.add(AuditLog(
                action="google_sheets_backend_sync_conflict",
                entity_type="order_item",
                entity_id=str(item.id),
                payload={
                    "order_id": str(item.order_id),
                    "reason": "backend item is missing from Google Sheets; VDS kept item active because Postgres is source of truth",
                    "source_import_id": (item.raw_payload or {}).get("source_import_id"),
                    "source_order_id": (item.raw_payload or {}).get("source_order_id"),
                    "scanned_blocks": item.scanned_blocks,
                },
            ))

def is_returned_record(record):
    status = normalize_text(record.get("return_status")).casefold()
    return status in {"возврат", "returned", "return"} or RETURN_STATUS_VALUE.casefold() in status


def main():
    interval = max(30, env_int("GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS", 60))
    once = normalize_text(os.environ.get("GOOGLE_SHEETS_SYNC_ONCE")).casefold() in {"1", "true", "yes", "да"}
    while True:
        try:
            with SessionLocal() as db:
                pending_result = process_pending_google_sheets_exports(db)
                if env_bool("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED", default=False):
                    result = sync_google_sheet_to_backend(db)
                else:
                    result = {**build_result(rows=0), "status": "skipped"}
            logging.info(
                "Google Sheets sync: pending_synced=%s pending_failed=%s rows=%s matched=%s missing=%s orders_updated=%s items_updated=%s conflicts=%s archived=%s removed=%s",
                pending_result["synced"],
                pending_result["failed"],
                result["rows"],
                result["matched"],
                result["missing"],
                result["orders_updated"],
                result["items_updated"],
                result["conflicts"],
                result["archived"],
                result["removed"],
            )
        except Exception:
            logging.exception("Google Sheets sync worker failed")
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
