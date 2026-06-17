import hashlib
import json
import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import make_sheet_record
from .google_sheets_pending import (
    queue_google_sheets_export,
)
from .models import AuditLog, ImportFile, ImportJob, Incident, Order, OrderItem
from .orders_service import STATUS_COMPLETED, STATUS_NOT_COMPLETED
from .schemas import ImportCreate, ImportRead, ImportResult
from .skladbot_request_dry_run import create_skladbot_dry_run_for_import


logger = logging.getLogger(__name__)

ORDER_DATE_FIELDS = ("Дата отгрузки", "Дата получения заказа", "order_date", "date")
PAYMENT_FIELDS = ("Тип оплаты", "payment_type", "payment")
CLIENT_FIELDS = ("Клиент", "client")
ADDRESS_FIELDS = ("Адрес", "address")
COORDINATES_FIELDS = ("Координаты", "coordinates")
REPRESENTATIVE_FIELDS = ("Торговый представитель", "representative")
PRODUCT_FIELDS = ("Товары", "product")
QUANTITY_PIECES_FIELDS = ("Кол-во ШТ", "quantity_pieces", "quantity")
QUANTITY_BLOCKS_FIELDS = ("Кол-во блок", "Кол-во блоков", "quantity_blocks", "blocks")
PIECES_PER_BLOCK_FIELDS = ("_pieces_per_block", "pieces_per_block")
BLOCK_PRICE_FIELDS = ("Цена за блок", "block_price")
IMPORTED_UNIT_PRICE_FIELDS = ("Цена из файла", "unit_price")
IMPORTED_LINE_TOTAL_FIELDS = ("Сумма из файла", "imported_line_total")
LINE_TOTAL_FIELDS = ("Сумма позиции", "line_total")
CALCULATED_LINE_TOTAL_FIELDS = ("Сумма рассчитанная", "calculated_line_total")
STATUS_FIELDS = ("Статус", "status")
ORDER_ID_FIELDS = ("ID заказа", "order_id", "external_id")
IMPORT_ID_FIELDS = ("ID импорта", "import_id")
SOURCE_FILE_FIELDS = ("Источник файла", "source_file")
SOURCE_ROW_FIELDS = ("Строка файла", "source_row")
SKLADBOT_NUMBER_FIELDS = ("Номер заявки SkladBot", "skladbot_request_number")
SKLADBOT_ID_FIELDS = ("ID заявки SkladBot", "skladbot_request_id")
PICKUP_ADDRESS = "Самовывоз со склада"
MISSING_ADDRESS_MARKERS = {
    "адрес не указан",
    "адрес не найден",
    "адреса не найдены",
    "адрес не определен",
    "адрес отсутствует",
    "самовывоз",
    "самовывоз со склада",
    "нет",
    "n/a",
    "na",
    "null",
    "none",
    "-",
    "—",
}


class ImportRowError(Exception):
    pass


def create_import(db: Session, payload: ImportCreate):
    rows_total = len(payload.rows)
    errors = []
    duplicate_rows = 0
    invalid_rows = 0
    orders_created = 0
    items_created = 0
    backend_address_updates = 0
    google_sheets_records = []

    import_job = ImportJob(
        source=normalize_text(payload.source) or "excel",
        status="created",
        rows_total=rows_total,
        rows_imported=0,
        raw_payload={
            "filename": payload.filename,
            "sha256": normalize_text(payload.sha256).lower(),
            "telegram_chat_id": normalize_text(payload.telegram_chat_id),
            "telegram_event_id": normalize_text(payload.telegram_event_id),
            "orders_created": 0,
            "items_created": 0,
            "duplicate_rows": 0,
            "invalid_rows": 0,
            "errors": [],
        },
    )
    db.add(import_job)
    db.flush()

    if payload.filename and payload.sha256:
        normalized_sha = normalize_text(payload.sha256).lower()
        existing_file = db.execute(select(ImportFile).where(ImportFile.sha256 == normalized_sha)).scalar_one_or_none()
        if existing_file is None:
            db.add(ImportFile(
                import_id=import_job.id,
                filename=payload.filename,
                sha256=normalized_sha,
                size_bytes=0,
            ))

    order_by_key, item_keys, source_import_ids, existing_items = load_existing_import_keys(db)
    for index, raw_row in enumerate(payload.rows, start=1):
        try:
            row = normalize_import_row(raw_row)
        except ImportRowError as exc:
            invalid_rows += 1
            errors.append(f"row {index}: {exc}")
            continue

        google_sheets_records.append(
            make_sheet_record(
                row,
                item_key=row["item_key"],
                filename=payload.filename or "",
            )
        )

        existing_item = find_existing_item_for_row(row, existing_items)
        if existing_item is not None:
            duplicate_rows += 1
            if update_existing_order_address(existing_item.order, row):
                backend_address_updates += 1
            continue

        order = order_by_key.get(row["order_key"])
        if order is None:
            order = Order(
                source=import_job.source,
                external_id=row["order_key"],
                order_date=row["order_date"],
                payment_type=row["payment_type"],
                client=row["client"],
                address=row["address"],
                representative=row["representative"],
                status=row["status"],
                raw_payload={
                    "order_key": row["order_key"],
                    "skladbot_request_number": row["skladbot_request_number"],
                    "skladbot_request_id": row["skladbot_request_id"],
                    "coordinates": row["coordinates"],
                    "source": import_job.source,
                },
            )
            db.add(order)
            db.flush()
            order_by_key[row["order_key"]] = order
            orders_created += 1

        db.add(OrderItem(
            order_id=order.id,
            product=row["product"],
            quantity_pieces=row["quantity_pieces"],
            quantity_blocks=row["quantity_blocks"],
            pieces_per_block=row["pieces_per_block"],
            scanned_blocks=0,
            requires_kiz=True,
            status=row["status"],
            raw_payload={
                "item_key": row["item_key"],
                "business_line_key": row["item_key"],
                "source_order_id": row["source_order_id"],
                "source_import_id": row["source_import_id"],
                "source_file": row["source_file"],
                "source_row": row["source_row"],
                "backend_import_id": str(import_job.id),
                "block_price": row["block_price"],
                "imported_unit_price": row["imported_unit_price"],
                "imported_line_total": row["imported_line_total"],
                "line_total": row["line_total"],
                "calculated_line_total": row["calculated_line_total"],
                "raw_row": raw_row,
            },
        ))
        item_keys.add(row["item_key"])
        if row["source_import_id"]:
            source_import_ids.add(row["source_import_id"])
        items_created += 1

    status = "completed"
    if errors and items_created:
        status = "completed_with_errors"
    elif errors and not items_created:
        status = "failed"

    import_job.status = status
    import_job.rows_imported = items_created
    import_job.raw_payload = {
        **import_job.raw_payload,
        "orders_created": orders_created,
        "items_created": items_created,
        "duplicate_rows": duplicate_rows,
        "invalid_rows": invalid_rows,
        "backend_address_updates": backend_address_updates,
        "errors": errors,
    }
    ensure_import_incident(db, import_job)
    db.add(AuditLog(
        action="orders_imported",
        entity_type="import",
        entity_id=str(import_job.id),
        payload=import_job.raw_payload,
    ))
    db.commit()
    db.refresh(import_job)
    google_sheets_result = export_import_records_to_google_sheets(
        db,
        google_sheets_records,
        import_job_id=str(import_job.id),
    )
    import_job.raw_payload = {
        **(import_job.raw_payload or {}),
        "google_sheets": google_sheets_result,
    }
    db.commit()
    db.refresh(import_job)
    import_job_id = import_job.id
    try:
        skladbot_dry_run_result = create_skladbot_dry_run_for_import(db, str(import_job_id))
        import_job.raw_payload = {
            **(import_job.raw_payload or {}),
            "skladbot_dry_run": skladbot_dry_run_result,
        }
        db.commit()
        db.refresh(import_job)
    except Exception as exc:
        db.rollback()
        logger.exception("SkladBot dry-run failed for import %s", import_job_id)
        skladbot_dry_run_result = {
            "status": "error",
            "mode": "dry_run",
            "orders": 0,
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "event_id": "",
            "error": str(exc)[:500],
        }
        import_job = db.get(ImportJob, import_job_id)
        import_job.raw_payload = {
            **(import_job.raw_payload or {}),
            "skladbot_dry_run": skladbot_dry_run_result,
        }
        db.add(AuditLog(
            action="skladbot_request_dry_run_failed",
            entity_type="import",
            entity_id=str(import_job_id),
            payload={
                "import_id": str(import_job_id),
                "status": "error",
                "error": str(exc)[:500],
            },
        ))
        db.commit()
        db.refresh(import_job)
    return ImportResult(
        id=str(import_job.id),
        source=import_job.source,
        status=import_job.status,
        rows_total=rows_total,
        rows_imported=items_created,
        orders_created=orders_created,
        items_created=items_created,
        duplicate_rows=duplicate_rows,
        invalid_rows=invalid_rows,
        errors=errors,
        backend_address_updates=backend_address_updates,
        google_sheets_status=google_sheets_result.get("status", ""),
        google_sheets_imported=google_sheets_result.get("imported", 0),
        google_sheets_duplicates=google_sheets_result.get("duplicates", 0),
        google_sheets_updated=google_sheets_result.get("updated", 0),
        google_sheets_error=google_sheets_result.get("error", ""),
        skladbot_dry_run_status=skladbot_dry_run_result.get("status", ""),
        skladbot_dry_run_ready=skladbot_dry_run_result.get("ready", 0),
        skladbot_dry_run_blocked=skladbot_dry_run_result.get("blocked", 0),
        skladbot_dry_run_already_linked=skladbot_dry_run_result.get("already_linked", 0),
        skladbot_dry_run_event_id=skladbot_dry_run_result.get("event_id", ""),
    )


def ensure_import_incident(db: Session, import_job: ImportJob):
    if import_job.status not in ("failed", "completed_with_errors"):
        return None
    existing = db.execute(
        select(Incident).where(Incident.import_id == import_job.id).where(Incident.source == "excel_import")
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    raw_payload = dict(import_job.raw_payload or {})
    errors = list(raw_payload.get("errors") or [])
    severity = "critical" if import_job.status == "failed" else "warning"
    title = "Excel import failed" if import_job.status == "failed" else "Excel import completed with errors"
    incident = Incident(
        source="excel_import",
        severity=severity,
        status="open",
        title=title,
        message="\n".join(str(error) for error in errors[:5]),
        entity_type="import",
        entity_id=str(import_job.id),
        pending_event_id=parse_optional_uuid(raw_payload.get("telegram_event_id")),
        import_id=import_job.id,
        raw_payload={
            "filename": normalize_text(raw_payload.get("filename")),
            "status": import_job.status,
            "rows_total": import_job.rows_total,
            "rows_imported": import_job.rows_imported,
            "invalid_rows": raw_payload.get("invalid_rows", 0),
            "duplicate_rows": raw_payload.get("duplicate_rows", 0),
            "errors": errors[:5],
        },
    )
    db.add(incident)
    db.add(AuditLog(
        action="import_incident_created",
        entity_type="import",
        entity_id=str(import_job.id),
        payload={
            "status": import_job.status,
            "incident_source": incident.source,
            "severity": incident.severity,
            "filename": normalize_text(raw_payload.get("filename")),
        },
    ))
    return incident


def export_import_records_to_google_sheets(db: Session, records, import_job_id=""):
    if not records:
        return {
            "status": "skipped",
            "imported": 0,
            "duplicates": 0,
            "updated": 0,
            "error": "",
        }
    result = {
        "status": "queued",
        "imported": 0,
        "duplicates": 0,
        "updated": 0,
        "error": "",
        "queued": True,
    }
    event = queue_google_sheets_export(
        db,
        "google_sheets_import_export",
        "import",
        import_job_id,
        result=result,
        payload={"records": records},
    )
    return {**result, "pending_event_id": str(event.id) if event else ""}


def list_imports(db: Session):
    stmt = select(ImportJob).order_by(ImportJob.created_at.desc())
    return [
        ImportRead(
            id=str(row.id),
            source=row.source,
            status=row.status,
            rows_total=row.rows_total,
            rows_imported=row.rows_imported,
            raw_payload=row.raw_payload,
            created_at=row.created_at,
        )
        for row in db.execute(stmt).scalars().all()
    ]


def load_existing_import_keys(db: Session):
    orders = db.execute(select(Order).options(selectinload(Order.items))).scalars().all()
    order_by_key = {}
    item_keys = set()
    source_import_ids = set()
    existing_items = {
        "item_key": {},
        "source_import_id": {},
    }
    for order in orders:
        order_key = (order.raw_payload or {}).get("order_key") or order.external_id
        if order_key:
            order_by_key[order_key] = order
        for item in order.items:
            raw_payload = item.raw_payload or {}
            item_key = raw_payload.get("item_key")
            if item_key:
                item_keys.add(item_key)
                existing_items["item_key"].setdefault(item_key, item)
            source_import_id = raw_payload.get("source_import_id")
            if source_import_id:
                source_import_ids.add(source_import_id)
                existing_items["source_import_id"].setdefault(source_import_id, item)
    return order_by_key, item_keys, source_import_ids, existing_items


def find_existing_item_for_row(row, existing_items):
    source_import_id = row.get("source_import_id")
    if source_import_id:
        item = existing_items["source_import_id"].get(source_import_id)
        if item is not None:
            return item
    item_key = row.get("item_key")
    if item_key:
        return existing_items["item_key"].get(item_key)
    return None


def update_existing_order_address(order, row):
    if order is None:
        return False
    new_address = normalize_text(row.get("address"))
    if not is_real_address(new_address) or not is_missing_address(order.address):
        return False

    order.address = new_address
    raw_payload = dict(order.raw_payload or {})
    if row.get("coordinates"):
        raw_payload["coordinates"] = row["coordinates"]
    raw_payload["address_backfilled_at"] = datetime.now().isoformat(timespec="seconds")
    raw_payload["address_backfill_source"] = normalize_text(row.get("source_file")) or "import"
    order.raw_payload = raw_payload
    return True


def normalize_import_row(raw_row):
    order_date = parse_date_value(first_value(raw_row, ORDER_DATE_FIELDS))
    payment_type = first_value(raw_row, PAYMENT_FIELDS)
    client = first_value(raw_row, CLIENT_FIELDS)
    address = normalize_import_address(first_value(raw_row, ADDRESS_FIELDS))
    coordinates = first_value(raw_row, COORDINATES_FIELDS)
    representative = first_value(raw_row, REPRESENTATIVE_FIELDS) or None
    product = first_value(raw_row, PRODUCT_FIELDS)
    quantity_pieces = parse_int(first_value(raw_row, QUANTITY_PIECES_FIELDS))
    quantity_blocks = parse_int(first_value(raw_row, QUANTITY_BLOCKS_FIELDS))
    pieces_per_block = parse_int(first_value(raw_row, PIECES_PER_BLOCK_FIELDS)) or 10
    if quantity_blocks <= 0 and quantity_pieces > 0:
        quantity_blocks = (quantity_pieces + pieces_per_block - 1) // pieces_per_block
    block_price = parse_money(first_value(raw_row, BLOCK_PRICE_FIELDS)) or default_block_price()
    imported_unit_price = parse_money(first_value(raw_row, IMPORTED_UNIT_PRICE_FIELDS))
    imported_line_total = parse_money(first_value(raw_row, IMPORTED_LINE_TOTAL_FIELDS))
    calculated_line_total = parse_money(first_value(raw_row, CALCULATED_LINE_TOTAL_FIELDS)) or quantity_blocks * block_price
    line_total = parse_money(first_value(raw_row, LINE_TOTAL_FIELDS)) or imported_line_total or calculated_line_total
    status = normalize_status(first_value(raw_row, STATUS_FIELDS))
    source_order_id = first_value(raw_row, ORDER_ID_FIELDS)
    source_import_id = first_value(raw_row, IMPORT_ID_FIELDS)
    source_file = first_value(raw_row, SOURCE_FILE_FIELDS)
    source_row = first_value(raw_row, SOURCE_ROW_FIELDS)
    skladbot_request_number = first_value(raw_row, SKLADBOT_NUMBER_FIELDS)
    skladbot_request_id = first_value(raw_row, SKLADBOT_ID_FIELDS)

    required = {
        "payment_type": payment_type,
        "client": client,
        "product": product,
    }
    missing = [name for name, value in required.items() if not normalize_text(value)]
    if missing:
        raise ImportRowError(f"missing required fields: {', '.join(missing)}")
    if quantity_pieces <= 0 and quantity_blocks <= 0:
        raise ImportRowError("quantity must be greater than zero")

    order_key = stable_hash({
        "date": order_date.isoformat() if order_date else "",
        "payment_type": payment_type,
        "client": client,
        "address": address,
        "coordinates": coordinates,
        "representative": representative,
        "skladbot_request_number": skladbot_request_number,
        "skladbot_request_id": skladbot_request_id,
    })
    item_key = stable_hash({
        "order_key": order_key,
        "product": product,
        "quantity_pieces": quantity_pieces,
        "quantity_blocks": quantity_blocks,
    })
    return {
        "order_key": order_key,
        "item_key": item_key,
        "order_date": order_date,
        "payment_type": normalize_text(payment_type),
        "client": normalize_text(client),
        "address": normalize_text(address),
        "coordinates": normalize_text(coordinates),
        "representative": normalize_text(representative) or None,
        "product": normalize_text(product),
        "quantity_pieces": quantity_pieces,
        "quantity_blocks": quantity_blocks,
        "pieces_per_block": pieces_per_block,
        "block_price": block_price,
        "imported_unit_price": imported_unit_price,
        "imported_line_total": imported_line_total,
        "line_total": line_total,
        "calculated_line_total": calculated_line_total,
        "status": status,
        "source_order_id": normalize_text(source_order_id),
        "source_import_id": normalize_text(source_import_id),
        "source_file": normalize_text(source_file),
        "source_row": normalize_text(source_row),
        "skladbot_request_number": normalize_text(skladbot_request_number),
        "skladbot_request_id": normalize_text(skladbot_request_id),
    }


def first_value(row, field_names):
    for field_name in field_names:
        if field_name in row:
            value = row.get(field_name)
            if normalize_text(value):
                return value
    return ""


def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_optional_uuid(value):
    text = normalize_text(value)
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def normalize_lookup_text(value):
    return normalize_text(value).casefold().replace("ё", "е")


def is_missing_address(value):
    text = normalize_lookup_text(value)
    return not text or text in MISSING_ADDRESS_MARKERS or text.startswith("координаты")


def is_real_address(value):
    text = normalize_lookup_text(value)
    return bool(text and not is_missing_address(text))


def normalize_import_address(value):
    text = normalize_text(value)
    return text if is_real_address(text) else PICKUP_ADDRESS


def parse_int(value):
    text = normalize_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_money(value):
    if isinstance(value, (int, float)):
        return int(value)
    text = normalize_text(value).replace("\xa0", " ").strip()
    if not text:
        return 0
    if text.replace(" ", "").replace(",", ".").replace(".", "", 1).isdigit():
        try:
            return int(float(text.replace(" ", "").replace(",", ".")))
        except ValueError:
            pass
    digits = "".join(char for char in text if char.isdigit())
    return int(digits) if digits else 0


def default_block_price():
    return 240000


def parse_date_value(value):
    text = normalize_text(value)
    if not text:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ImportRowError(f"invalid date: {text}")


def normalize_status(value):
    text = normalize_text(value).lower()
    if text in {"completed", "done", "closed", "выполнено", "готово", "1", "true", "yes"}:
        return STATUS_COMPLETED
    return STATUS_NOT_COMPLETED


def stable_hash(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
