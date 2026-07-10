import logging
import os
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

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
    lock_kiz_code_for_transaction,
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
from .scan_quantities import scan_metadata_for_code, scanned_blocks_for_scans
from .google_sheets_pending import (
    google_sheets_export_cooldown_until,
    is_google_rate_limit_error,
    process_pending_google_sheets_exports,
    retry_after_seconds_from_error,
)
from .imports_service import source_import_lookup_key


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


ORDER_DATE_COLUMN = "Дата отгрузки"
LEGACY_ORDER_DATE_COLUMN = "Дата получения заказа"
STATUS_COLUMN = "Статус"
DEFAULT_BLOCK_PRICE = 240000
GOOGLE_BACKEND_LOOKUP_BATCH_SIZE = 500
GOOGLE_BACKEND_RECONCILE_BATCH_SIZE = 200
GOOGLE_BACKEND_RECONCILE_CURSOR_ACTION = "google_sheets_backend_missing_reconciliation_cursor"
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


def backend_sync_interval_seconds(worker_interval=None):
    worker_interval = max(30, int(worker_interval or 60))
    return max(30, env_int("GOOGLE_SHEETS_BACKEND_SYNC_INTERVAL_SECONDS", max(worker_interval, 300)))


def backend_sync_rate_limit_cooldown_seconds(worker_interval=None):
    worker_interval = max(30, int(worker_interval or 60))
    return max(30, env_int("GOOGLE_SHEETS_BACKEND_SYNC_RATE_LIMIT_COOLDOWN_SECONDS", max(worker_interval, 300)))


def skipped_backend_sync_result(reason):
    return {**build_result(rows=0), "status": "skipped", "reason": reason}


def run_google_sheets_worker_cycle(
    db: Session,
    *,
    backend_sync_enabled=None,
    next_backend_sync_at=0.0,
    backend_sync_interval=None,
    rate_limit_cooldown=None,
    now_monotonic=None,
    pending_processor=process_pending_google_sheets_exports,
    cooldown_reader=google_sheets_export_cooldown_until,
    backend_syncer=None,
):
    now_monotonic = time.monotonic() if now_monotonic is None else float(now_monotonic)
    backend_sync_enabled = (
        env_bool("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED", default=False)
        if backend_sync_enabled is None
        else bool(backend_sync_enabled)
    )
    backend_sync_interval = max(30, int(backend_sync_interval or backend_sync_interval_seconds()))
    rate_limit_cooldown = max(30, int(rate_limit_cooldown or backend_sync_rate_limit_cooldown_seconds()))
    backend_syncer = sync_google_sheet_to_backend if backend_syncer is None else backend_syncer

    pending_result = pending_processor(db)
    if not backend_sync_enabled:
        return pending_result, skipped_backend_sync_result("disabled"), next_backend_sync_at
    if str((pending_result or {}).get("status") or "").strip().lower() == "paused":
        return (
            pending_result,
            skipped_backend_sync_result("pending_export_paused"),
            max(float(next_backend_sync_at or 0), now_monotonic + rate_limit_cooldown),
        )
    cooldown_until = cooldown_reader(db)
    if cooldown_until is not None:
        cooldown_seconds = max(1, int((cooldown_until - datetime.now(timezone.utc)).total_seconds()))
        return (
            pending_result,
            skipped_backend_sync_result("pending_export_cooldown"),
            max(float(next_backend_sync_at or 0), now_monotonic + cooldown_seconds),
        )
    if now_monotonic < float(next_backend_sync_at or 0):
        return pending_result, skipped_backend_sync_result("cooldown"), next_backend_sync_at

    try:
        result = backend_syncer(db)
    except Exception as exc:
        if not is_google_rate_limit_error(exc):
            raise
        cooldown = retry_after_seconds_from_error(exc, default=rate_limit_cooldown)
        logging.warning("Google Sheets backend sync paused after rate limit: %s", exc)
        return (
            pending_result,
            {**build_result(rows=0), "status": "paused", "reason": "rate_limited", "error": str(exc)},
            now_monotonic + cooldown,
        )
    return pending_result, result, now_monotonic + backend_sync_interval


def sync_google_sheet_to_backend(db: Session, sheet=None, now=None, archive_completed_data_rows=None):
    now = now or datetime.now(timezone.utc)
    if archive_completed_data_rows is None:
        archive_completed_data_rows = sheet is None
    records = load_google_sheet_records(sheet)
    if not records:
        return build_result(rows=0)

    item_index = load_item_index(db, records)
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

    if should_record_sync_summary(result):
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
    data_sheet = spreadsheet.worksheet(SHEET_NAME)
    ensure_import_sheet_layout(data_sheet)
    data_records = parse_sheet_records(data_sheet.get_all_values(), source_sheet=SHEET_NAME)

    try:
        archive_sheet = spreadsheet.worksheet(ARCHIVE_SHEET_NAME)
    except Exception:
        archive_sheet = None
    if archive_sheet is None:
        return data_records
    ensure_import_sheet_layout(archive_sheet)
    archive_records = parse_sheet_records(
        archive_sheet.get_all_values(),
        source_sheet=ARCHIVE_SHEET_NAME,
        archived=True,
    )
    return merge_google_sheet_records(data_records, archive_records)


def google_sheet_record_identity(record):
    source_import_id = normalize_text(record.get("source_import_id"))
    if source_import_id:
        return "source_import_id", source_import_id
    source_order_id = normalize_text(record.get("source_order_id"))
    if source_order_id:
        return "source_order_id", source_order_id
    return None


def merge_google_sheet_records(data_records, archive_records):
    data_records = list(data_records or [])
    active_keys = {
        key
        for record in data_records
        if (key := google_sheet_record_identity(record)) is not None
    }
    return [
        *data_records,
        *[
            record
            for record in (archive_records or [])
            if google_sheet_record_identity(record) not in active_keys
        ],
    ]


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


def load_item_index(db: Session, records=None, batch_size=GOOGLE_BACKEND_LOOKUP_BATCH_SIZE):
    source_import_ids = sorted({
        normalize_text(record.get("source_import_id"))
        for record in records or []
        if normalize_text(record.get("source_import_id"))
    })
    source_order_ids = sorted({
        normalize_text(record.get("source_order_id"))
        for record in records or []
        if normalize_text(record.get("source_order_id"))
    })
    index = {
        "source_import_id": {},
        "source_order_id": {},
        "_sheet_source_import_ids": set(source_import_ids),
        "_sheet_source_order_ids": set(source_order_ids),
    }
    batch_size = max(1, min(int(batch_size or GOOGLE_BACKEND_LOOKUP_BATCH_SIZE), 1000))
    seen_item_ids = set()

    def add_items(items):
        for item in items:
            if item.id in seen_item_ids:
                continue
            seen_item_ids.add(item.id)
            raw_payload = item.raw_payload or {}
            source_import_id = normalize_text(item.source_import_id) or normalize_text(raw_payload.get("source_import_id"))
            source_order_id = normalize_text(raw_payload.get("source_order_id"))
            if source_import_id:
                index["source_import_id"].setdefault(source_import_id, item)
            if source_order_id:
                index["source_order_id"].setdefault(source_order_id, item)

    options = (joinedload(OrderItem.order), selectinload(OrderItem.scan_codes))
    for start in range(0, len(source_import_ids), batch_size):
        batch = source_import_ids[start:start + batch_size]
        source_keys = [source_import_lookup_key(value) for value in batch]
        add_items(db.execute(
            select(OrderItem)
            .options(*options)
            .where(OrderItem.source_import_key.in_(source_keys))
            .order_by(OrderItem.created_at, OrderItem.id)
        ).scalars().all())
        missing = [value for value in batch if value not in index["source_import_id"]]
        if missing:
            legacy_value = OrderItem.raw_payload["source_import_id"].as_string()
            ranked = (
                select(
                    OrderItem.id.label("item_id"),
                    func.row_number().over(
                        partition_by=legacy_value,
                        order_by=(OrderItem.created_at, OrderItem.id),
                    ).label("identity_rank"),
                )
                .where(OrderItem.source_import_key.is_(None))
                .where(legacy_value.in_(missing))
                .subquery()
            )
            add_items(db.execute(
                select(OrderItem)
                .options(*options)
                .join(ranked, ranked.c.item_id == OrderItem.id)
                .where(ranked.c.identity_rank == 1)
                .order_by(OrderItem.created_at, OrderItem.id)
            ).scalars().all())

    for start in range(0, len(source_order_ids), batch_size):
        batch = [value for value in source_order_ids[start:start + batch_size] if value not in index["source_order_id"]]
        if not batch:
            continue
        source_order_value = OrderItem.raw_payload["source_order_id"].as_string()
        ranked = (
            select(
                OrderItem.id.label("item_id"),
                func.row_number().over(
                    partition_by=source_order_value,
                    order_by=(OrderItem.created_at, OrderItem.id),
                ).label("identity_rank"),
            )
            .where(source_order_value.in_(batch))
            .subquery()
        )
        add_items(db.execute(
            select(OrderItem)
            .options(*options)
            .join(ranked, ranked.c.item_id == OrderItem.id)
            .where(ranked.c.identity_rank == 1)
            .order_by(OrderItem.created_at, OrderItem.id)
        ).scalars().all())
    return index


def load_missing_reconciliation_batch(db: Session, cursor="", batch_size=GOOGLE_BACKEND_RECONCILE_BATCH_SIZE):
    """Load at most one stable batch; the caller persists the next cursor."""
    batch_size = max(1, min(int(batch_size or GOOGLE_BACKEND_RECONCILE_BATCH_SIZE), 1000))
    terminal_statuses = (
        *COMPLETED_STATUSES,
        STATUS_ARCHIVED_NO_KIZ,
        STATUS_CANCELLED,
        STATUS_REMOVED_FROM_GOOGLE,
    )
    source_order_id = OrderItem.raw_payload["source_order_id"].as_string()

    def execute_batch(after_cursor):
        stmt = (
            select(OrderItem)
            .where(~OrderItem.status.in_(terminal_statuses))
            .where(or_(
                OrderItem.source_import_id.is_not(None),
                and_(source_order_id.is_not(None), source_order_id != ""),
            ))
            .order_by(OrderItem.id)
            .limit(batch_size)
        )
        if after_cursor:
            try:
                stmt = stmt.where(OrderItem.id > uuid.UUID(str(after_cursor)))
            except (TypeError, ValueError):
                pass
        return db.execute(stmt).scalars().all()

    items = execute_batch(cursor)
    if not items and cursor:
        items = execute_batch("")
    return items


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
        add_sync_conflict_audit(
            db,
            entity_id=str(item.id),
            payload={
                "order_id": str(order.id),
                "row_number": record["row_number"],
                "source_import_id": record.get("source_import_id"),
                "conflicts": conflicts,
            },
        )

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
        ("representative", record.get("representative")),
    ):
        if value in (None, ""):
            continue
        if getattr(order, field_name) != value:
            setattr(order, field_name, value)
            changed = True

    incoming_address = record.get("address")
    if should_update_address_from_google(order.address, incoming_address):
        order.address = incoming_address
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


def should_update_address_from_google(current_address, incoming_address):
    incoming = normalize_text(incoming_address)
    if not incoming:
        return False
    incoming_missing = is_missing_sheet_address(incoming)
    if incoming_missing:
        return False
    return normalize_text(current_address) != incoming


def is_missing_sheet_address(value):
    text = normalize_text(value).casefold().replace("ё", "е")
    return (
        not text
        or text in {
            "адрес не указан",
            "адрес не найден",
            "адреса не найдены",
            "адрес не определен",
            "адрес отсутствует",
            "самовывоз",
            "самовывоз со склада",
        }
        or text.startswith(("координаты", "gps"))
    )


def apply_skladbot_fields(raw_payload, record):
    incoming_number = normalize_text(record.get("skladbot_request_number"))
    incoming_id = normalize_text(record.get("skladbot_request_id"))
    incoming_status = normalize_text(record.get("skladbot_status"))
    existing_has_link = bool(
        normalize_text(raw_payload.get("skladbot_request_number"))
        or normalize_text(raw_payload.get("skladbot_request_id"))
    )
    incoming_has_link = bool(incoming_number or incoming_id)

    if incoming_number:
        raw_payload["skladbot_request_number"] = incoming_number
    if incoming_id:
        raw_payload["skladbot_request_id"] = incoming_id
    if incoming_status and (incoming_has_link or not existing_has_link):
        raw_payload["skladbot_status"] = incoming_status


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
    imported_line_total = parse_int_value(raw_payload.get("imported_line_total"))
    blocks = int(quantity_blocks or record.get("quantity_blocks") or 0)
    calculated_line_total = blocks * block_price if blocks > 0 and block_price > 0 else 0
    if calculated_line_total > 0:
        raw_payload["calculated_line_total"] = calculated_line_total
        if sheet_line_total > 0:
            raw_payload["line_total"] = sheet_line_total
            if sheet_line_total != calculated_line_total:
                raw_payload["google_sheet_line_total"] = sheet_line_total
        elif imported_line_total > 0:
            raw_payload["line_total"] = imported_line_total
        else:
            raw_payload["line_total"] = calculated_line_total
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
        lock_kiz_code_for_transaction(db, code)
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
                **scan_metadata_for_code(code),
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

    new_scanned_blocks = max(item.scanned_blocks, scanned_blocks_for_scans(item.scan_codes))
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
    cursor = load_missing_reconciliation_cursor(db)
    batch = load_missing_reconciliation_batch(db, cursor=cursor)
    sheet_import_ids = set(item_index.get("_sheet_source_import_ids") or ())
    sheet_order_ids = set(item_index.get("_sheet_source_order_ids") or ())
    missing_items = []
    for item in batch:
        raw_payload = item.raw_payload or {}
        source_import_id = normalize_text(item.source_import_id) or normalize_text(raw_payload.get("source_import_id"))
        source_order_id = normalize_text(raw_payload.get("source_order_id"))
        if item.id in matched_item_ids:
            continue
        if source_import_id and source_import_id in sheet_import_ids:
            continue
        if not source_import_id and source_order_id and source_order_id in sheet_order_ids:
            continue
        missing_items.append(item)

    previous_by_entity = latest_missing_conflict_audits(db, [str(item.id) for item in missing_items])
    for item in missing_items:
        result["conflicts"] += 1
        entity_id = str(item.id)
        payload = {
            "order_id": str(item.order_id),
            "reason": "backend item is missing from Google Sheets; VDS kept item active because Postgres is source of truth",
            "source_import_id": item.source_import_id or (item.raw_payload or {}).get("source_import_id"),
            "source_order_id": (item.raw_payload or {}).get("source_order_id"),
            "scanned_blocks": item.scanned_blocks,
        }
        previous = previous_by_entity.get(entity_id)
        if previous is None or (previous.payload or {}) != payload:
            db.add(AuditLog(
                action="google_sheets_backend_sync_conflict",
                entity_type="order_item",
                entity_id=entity_id,
                payload=payload,
            ))

    if batch:
        db.add(AuditLog(
            action=GOOGLE_BACKEND_RECONCILE_CURSOR_ACTION,
            entity_type="google_sheets",
            entity_id=SHEET_NAME,
            payload={
                "cursor": str(batch[-1].id),
                "checked": len(batch),
                "missing": len(missing_items),
                "checked_at": now.isoformat(),
            },
        ))


def load_missing_reconciliation_cursor(db: Session):
    row = db.execute(
        select(AuditLog)
        .where(AuditLog.action == GOOGLE_BACKEND_RECONCILE_CURSOR_ACTION)
        .where(AuditLog.entity_type == "google_sheets")
        .where(AuditLog.entity_id == SHEET_NAME)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return normalize_text((row.payload or {}).get("cursor")) if row is not None else ""


def latest_missing_conflict_audits(db: Session, entity_ids):
    entity_ids = list(dict.fromkeys(entity_ids or []))
    if not entity_ids:
        return {}
    row = aliased(AuditLog)
    candidate = aliased(AuditLog)
    latest_id = (
        select(candidate.id)
        .where(candidate.action == "google_sheets_backend_sync_conflict")
        .where(candidate.entity_type == "order_item")
        .where(candidate.entity_id == row.entity_id)
        .order_by(candidate.created_at.desc(), candidate.id.desc())
        .limit(1)
        .correlate(row)
        .scalar_subquery()
    )
    rows = db.execute(
        select(row)
        .where(row.action == "google_sheets_backend_sync_conflict")
        .where(row.entity_type == "order_item")
        .where(row.entity_id.in_(entity_ids))
        .where(row.id == latest_id)
    ).scalars().all()
    return {audit.entity_id: audit for audit in rows}


def should_record_sync_summary(result):
    return any(
        int(result.get(field) or 0) > 0
        for field in ("orders_updated", "items_updated", "archived", "removed")
    )


def add_sync_conflict_audit(db: Session, *, entity_id, payload):
    previous = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "google_sheets_backend_sync_conflict")
        .where(AuditLog.entity_type == "order_item")
        .where(AuditLog.entity_id == entity_id)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(1)
    ).scalar_one_or_none()
    if previous is not None and (previous.payload or {}) == payload:
        return False
    db.add(AuditLog(
        action="google_sheets_backend_sync_conflict",
        entity_type="order_item",
        entity_id=entity_id,
        payload=payload,
    ))
    return True


def is_returned_record(record):
    status = normalize_text(record.get("return_status")).casefold()
    return status in {"возврат", "returned", "return"} or RETURN_STATUS_VALUE.casefold() in status


def main():
    interval = max(30, env_int("GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS", 60))
    once = normalize_text(os.environ.get("GOOGLE_SHEETS_SYNC_ONCE")).casefold() in {"1", "true", "yes", "да"}
    backend_sync_interval = backend_sync_interval_seconds(interval)
    rate_limit_cooldown = backend_sync_rate_limit_cooldown_seconds(interval)
    next_backend_sync_at = 0.0
    while True:
        try:
            with SessionLocal() as db:
                pending_result, result, next_backend_sync_at = run_google_sheets_worker_cycle(
                    db,
                    backend_sync_interval=backend_sync_interval,
                    rate_limit_cooldown=rate_limit_cooldown,
                    next_backend_sync_at=next_backend_sync_at,
                )
            logging.info(
                "Google Sheets sync: pending_status=%s backend_status=%s pending_synced=%s pending_failed=%s rows=%s matched=%s missing=%s orders_updated=%s items_updated=%s conflicts=%s archived=%s removed=%s",
                pending_result.get("status", ""),
                result.get("status", "completed"),
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
