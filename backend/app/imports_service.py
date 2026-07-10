import hashlib
import json
import logging
import uuid
from datetime import date, datetime

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from .client_points_service import (
    prefetch_client_points_for_import,
    sync_client_point_from_import_row_cached,
)
from .google_sheets_exporter import make_sheet_record
from .google_sheets_pending import (
    queue_google_sheets_export,
)
from .models import AuditLog, ImportFile, ImportJob, Incident, Order, OrderItem, PendingEvent
from .pagination import CursorError, decode_cursor, encode_cursor, normalize_page_limit
from . import outbox_service
from .orders_service import STATUS_COMPLETED, STATUS_NOT_COMPLETED, STATUS_RETURNED
from .schemas import ImportCreate, ImportPreviewResult, ImportRead, ImportResult
from .skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    create_skladbot_dry_run_for_import,
    skladbot_create_idempotency_key,
)


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
IMPORTED_LINE_TOTAL_FIELDS = ("Сумма из файла", "Сумма с переоценкой", "imported_line_total")
LINE_TOTAL_FIELDS = ("Сумма позиции", "line_total")
CALCULATED_LINE_TOTAL_FIELDS = ("Сумма рассчитанная", "calculated_line_total")
STATUS_FIELDS = ("Статус", "status")
ORDER_ID_FIELDS = ("ID заказа", "order_id", "external_id")
IMPORT_ID_FIELDS = ("ID импорта", "import_id")
SOURCE_FILE_FIELDS = ("Источник файла", "source_file")
SOURCE_ROW_FIELDS = ("Строка файла", "source_row")
SOURCE_BATCH_FIELDS = ("Ключ исходного документа", "source_batch_key")
SKLADBOT_NUMBER_FIELDS = ("Номер заявки SkladBot", "skladbot_request_number")
SKLADBOT_ID_FIELDS = ("ID заявки SkladBot", "skladbot_request_id")
SMARTUP_AUTO_IMPORT_SOURCE = "smartup_auto"
LINKED_SKLADBOT_SPLIT_REASON = "linked_skladbot_late_smartup_export"
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


def create_import(db: Session, payload: ImportCreate, *, skladbot_create_mode: str | None = None):
    rows_total = len(payload.rows)
    normalized_sha = normalize_text(payload.sha256).lower()
    existing_file = None
    if normalized_sha:
        acquire_import_identity_lock(db, "file", normalized_sha)
        existing_file = db.execute(
            select(ImportFile).where(ImportFile.sha256 == normalized_sha).limit(1)
        ).scalar_one_or_none()

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
            "sha256": normalized_sha,
            "file_sha256_reused_from_import_id": (
                str(existing_file.import_id) if existing_file is not None and existing_file.import_id else ""
            ),
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

    if payload.filename and normalized_sha and existing_file is None:
        db.add(ImportFile(
            import_id=import_job.id,
            filename=payload.filename,
            sha256=normalized_sha,
            size_bytes=0,
        ))

    order_by_key = {}
    existing_items = {"item_key": {}, "source_import_id": {}}
    current_import_item_keys = set()
    current_import_source_import_ids = set()
    prepared_rows = []
    row_locks = set()
    for index, raw_row in enumerate(payload.rows, start=1):
        try:
            row = normalize_import_row(raw_row)
        except ImportRowError as exc:
            invalid_rows += 1
            errors.append(f"row {index}: {exc}")
            continue
        prepared_rows.append((index, raw_row, row))
        identity_value = normalize_text(row.get("source_import_id")) or normalize_text(row.get("item_key"))
        if identity_value:
            row_locks.add(("item", identity_value))
        if row.get("order_key"):
            row_locks.add(("order", row["order_key"]))

    acquire_import_identity_locks(db, row_locks)
    prefetch_existing_items(db, (row for _index, _raw, row in prepared_rows), existing_items)
    prefetch_active_orders(db, (row["order_key"] for _index, _raw, row in prepared_rows), order_by_key)
    client_points_by_key = prefetch_client_points_for_import(
        db,
        (row for _index, _raw, row in prepared_rows),
    )

    for _index, raw_row, row in prepared_rows:

        if row_is_duplicate_in_current_import(row, current_import_source_import_ids, current_import_item_keys):
            duplicate_rows += 1
            continue

        sync_client_point_from_import_row_cached(db, row, client_points_by_key)

        google_sheets_records.append(
            make_sheet_record(
                row,
                item_key=row["item_key"],
                filename=payload.filename or "",
            )
        )
        if row["item_key"]:
            current_import_item_keys.add(row["item_key"])
        if row["source_import_id"]:
            current_import_source_import_ids.add(row["source_import_id"])

        existing_item = find_existing_item_for_row(db, row, existing_items)
        if existing_item is not None:
            duplicate_rows += 1
            if update_existing_order_address(existing_item.order, row):
                backend_address_updates += 1
            continue

        source_order_key = row["order_key"]
        source_order = find_active_order_by_key(db, source_order_key, order_by_key)
        source_order_key, order_key, split_from_order = resolve_order_key_for_import_row(
            db, row, import_job.source, {source_order_key: source_order} if source_order else {},
        )
        if order_key != source_order_key:
            acquire_import_identity_lock(db, "order", order_key)
        order = find_active_order_by_key(db, order_key, order_by_key)
        if order is None:
            order = Order(
                id=uuid.uuid4(),
                source=import_job.source,
                external_id=order_key,
                import_order_key=order_key,
                import_source_order_key=source_order_key,
                order_date=row["order_date"],
                payment_type=row["payment_type"],
                client=row["client"],
                address=row["address"],
                representative=row["representative"],
                status=row["status"],
                raw_payload=build_order_raw_payload(
                    row,
                    import_job.source,
                    order_key=order_key,
                    source_order_key=source_order_key,
                    split_from_order=split_from_order,
                ),
            )
            db.add(order)
            order_by_key[order_key] = order
            orders_created += 1

        db.add(OrderItem(
            order_id=order.id,
            product=row["product"],
            import_item_key=row["item_key"] or None,
            source_import_key=source_import_lookup_key(row["source_import_id"]),
            source_import_id=row["source_import_id"] or None,
            source_batch_key=row["source_batch_key"] or None,
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
                "source_batch_key": row["source_batch_key"],
                "backend_import_id": str(import_job.id),
                "block_price": row["block_price"],
                "imported_unit_price": row["imported_unit_price"],
                "imported_line_total": row["imported_line_total"],
                "line_total": row["line_total"],
                "calculated_line_total": row["calculated_line_total"],
                "raw_row": raw_row,
            },
        ))
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
    google_sheets_result = export_import_records_to_google_sheets(
        db,
        google_sheets_records,
        import_job_id=str(import_job.id),
    )
    import_job.raw_payload = {
        **(import_job.raw_payload or {}),
        "google_sheets": google_sheets_result,
    }
    db.flush()
    try:
        with db.begin_nested():
            skladbot_dry_run_result = create_skladbot_dry_run_for_import(
                db,
                str(import_job.id),
                force_mode=skladbot_create_mode,
            )
    except Exception as exc:
        logger.exception("SkladBot dry-run failed for import %s", import_job.id)
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
        db.add(AuditLog(
            action="skladbot_request_dry_run_failed",
            entity_type="import",
            entity_id=str(import_job.id),
            payload={
                "import_id": str(import_job.id),
                "status": "error",
                "error": str(exc)[:500],
            },
        ))
    import_job.raw_payload = {
        **(import_job.raw_payload or {}),
        "skladbot_dry_run": skladbot_dry_run_result,
    }
    outbox_service.outbox_fault("before_commit", "import")
    db.commit()
    outbox_service.outbox_fault("after_commit", "import")
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
        skladbot_dry_run_linked_mismatch=skladbot_dry_run_result.get("linked_mismatch", 0),
        skladbot_dry_run_event_id=skladbot_dry_run_result.get("event_id", ""),
    )


def preview_import(db: Session, payload: ImportCreate):
    rows_total = len(payload.rows)
    errors = []
    duplicate_row_numbers = []
    invalid_row_numbers = []
    orders_new = 0
    items_new = 0
    backend_address_updates = 0

    order_by_key = {}
    existing_items = {"item_key": {}, "source_import_id": {}}
    preview_order_keys = set()
    preview_item_keys = set()
    preview_source_import_ids = set()

    for index, raw_row in enumerate(payload.rows, start=1):
        try:
            row = normalize_import_row(raw_row)
        except ImportRowError as exc:
            invalid_row_numbers.append(index)
            errors.append(f"row {index}: {exc}")
            continue

        existing_item = find_existing_item_for_row(db, row, existing_items)
        duplicate = existing_item is not None or row_is_duplicate_in_current_import(
            row,
            preview_source_import_ids,
            preview_item_keys,
        )

        if duplicate:
            duplicate_row_numbers.append(index)
            if existing_item is not None and should_update_existing_order_address(existing_item.order, row):
                backend_address_updates += 1
            continue

        source_order_key = row["order_key"]
        source_order = find_active_order_by_key(db, source_order_key, order_by_key)
        if source_order is not None:
            preview_order_keys.add(source_order_key)
        _source_order_key, order_key, _split_from_order = resolve_order_key_for_import_row(
            db, row, payload.source, {source_order_key: source_order} if source_order else {},
        )
        existing_order = find_active_order_by_key(db, order_key, order_by_key)
        if existing_order is not None:
            preview_order_keys.add(order_key)
        if order_key not in preview_order_keys:
            orders_new += 1
            preview_order_keys.add(order_key)
        preview_item_keys.add(row["item_key"])
        if row["source_import_id"]:
            preview_source_import_ids.add(row["source_import_id"])
        items_new += 1

    status = "ok"
    if invalid_row_numbers and items_new:
        status = "ok_with_errors"
    elif invalid_row_numbers and not items_new:
        status = "failed"

    return ImportPreviewResult(
        source=normalize_text(payload.source) or "excel",
        status=status,
        rows_total=rows_total,
        rows_importable=items_new,
        orders_new=orders_new,
        items_new=items_new,
        duplicate_rows=len(duplicate_row_numbers),
        invalid_rows=len(invalid_row_numbers),
        duplicate_row_numbers=duplicate_row_numbers,
        invalid_row_numbers=invalid_row_numbers,
        errors=errors,
        backend_address_updates=backend_address_updates,
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
    chunks = chunk_outbox_records(records)
    events = [
        queue_google_sheets_export(
            db,
            "google_sheets_import_export",
            "import",
            import_job_id,
            result=result,
            payload={
                "records": chunk,
                "chunk_index": index,
                "chunk_count": len(chunks),
            },
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    event_ids = [str(event.id) for event in events if event is not None]
    return {
        **result,
        "pending_event_id": event_ids[0] if event_ids else "",
        "pending_event_ids": event_ids,
        "events_queued": len(event_ids),
    }


def chunk_outbox_records(records):
    maximum = outbox_service.MAX_OUTBOX_PAYLOAD_BYTES - (64 * 1024)
    chunks = []
    current = []
    current_bytes = len(b'{"records":[]}')
    for record in records:
        sanitized_record = outbox_service.sanitize_outbox_payload(record)
        encoded_record = json.dumps(
            sanitized_record, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
        ).encode("utf-8")
        separator_bytes = 1 if current else 0
        if current and current_bytes + separator_bytes + len(encoded_record) > maximum:
            chunks.append(current)
            current = [record]
            current_bytes = len(b'{"records":[]}') + len(encoded_record)
        else:
            current.append(record)
            current_bytes += separator_bytes + len(encoded_record)
    if current:
        chunks.append(current)
    return chunks


def list_imports_page(
    db: Session,
    *,
    limit=50,
    cursor="",
    import_id="",
    telegram_event_id="",
):
    row_limit = normalize_page_limit(limit)
    filters = {
        "import_id": normalize_text(import_id),
        "telegram_event_id": normalize_text(telegram_event_id),
    }
    stmt = select(ImportJob)
    if filters["import_id"]:
        try:
            stmt = stmt.where(ImportJob.id == uuid.UUID(filters["import_id"]))
        except ValueError:
            raise CursorError("invalid_cursor") from None
    if filters["telegram_event_id"]:
        stmt = stmt.where(
            ImportJob.raw_payload["telegram_event_id"].as_string() == filters["telegram_event_id"]
        )
    if cursor:
        try:
            cursor_created_at, cursor_id = decode_cursor(cursor, "imports", filters=filters)
            parsed_created_at = datetime.fromisoformat(str(cursor_created_at).replace("Z", "+00:00"))
            parsed_id = uuid.UUID(str(cursor_id))
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
        stmt = stmt.where(or_(
            ImportJob.created_at < parsed_created_at,
            and_(ImportJob.created_at == parsed_created_at, ImportJob.id < parsed_id),
        ))
    rows = db.execute(
        stmt.order_by(ImportJob.created_at.desc(), ImportJob.id.desc()).limit(row_limit + 1)
    ).scalars().all()
    page_rows = rows[:row_limit]
    result = [
        ImportRead(
            id=str(row.id),
            source=row.source,
            status=row.status,
            rows_total=row.rows_total,
            rows_imported=row.rows_imported,
            raw_payload=row.raw_payload,
            created_at=row.created_at,
        )
        for row in page_rows
    ]
    next_cursor = ""
    if len(rows) > row_limit:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            "imports",
            [last.created_at.isoformat(), str(last.id)],
            filters=filters,
        )
    return result, next_cursor, row_limit


def list_imports(db: Session, limit=None):
    return list_imports_page(db, limit=limit or 200)[0]


def acquire_import_identity_lock(db: Session, namespace: str, value: str):
    identity = f"taksklad:import:{normalize_text(namespace)}:{normalize_text(value)}"
    if not identity.rsplit(":", 1)[-1]:
        return
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), {"identity": identity})


def acquire_import_identity_locks(db: Session, locks):
    identities = sorted({
        f"taksklad:import:{normalize_text(namespace)}:{normalize_text(value)}"
        for namespace, value in locks
        if normalize_text(value)
    })
    if not identities or db.bind is None or db.bind.dialect.name != "postgresql":
        return
    result = db.execute(text(
        "WITH identities AS (SELECT unnest(CAST(:identities AS text[])) AS identity) "
        "SELECT pg_advisory_xact_lock(hashtextextended(identity, 0)) FROM identities ORDER BY identity"
    ), {"identities": identities})
    for _row in result:
        pass


def active_order_predicate():
    return_status = Order.raw_payload["return_status"].as_string()
    return and_(
        func.lower(Order.status) != STATUS_RETURNED,
        or_(
            return_status.is_(None),
            ~func.lower(return_status).in_(("returned", "return", "возврат")),
        ),
    )


def find_active_order_by_key(db: Session, order_key: str, cache=None):
    order_key = normalize_text(order_key)
    if not order_key:
        return None
    cache = cache if cache is not None else {}
    if order_key in cache:
        return cache[order_key]
    legacy_key = Order.raw_payload["order_key"].as_string()
    order = db.execute(
        select(Order)
        .where(active_order_predicate())
        .where(or_(
            Order.import_order_key == order_key,
            and_(
                Order.import_order_key.is_(None),
                or_(legacy_key == order_key, Order.external_id == order_key),
            ),
        ))
        .order_by(Order.created_at.asc(), Order.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    cache[order_key] = order
    return order


def prefetch_active_orders(db: Session, order_keys, cache):
    keys = {normalize_text(value) for value in order_keys if normalize_text(value)}
    if not keys:
        return
    cache.update({key: None for key in keys if key not in cache})
    legacy_key = Order.raw_payload["order_key"].as_string()
    statement = (
        select(Order)
        .where(active_order_predicate())
        .where(or_(
            Order.import_order_key.in_(keys),
            and_(Order.import_order_key.is_(None), or_(legacy_key.in_(keys), Order.external_id.in_(keys))),
        ))
        .order_by(Order.created_at.asc(), Order.id.asc())
    )
    for order in db.execute(statement).scalars():
        key = normalize_text(order.import_order_key)
        if not key:
            raw_payload = order.raw_payload or {}
            key = normalize_text(raw_payload.get("order_key")) or normalize_text(order.external_id)
        if key and cache.get(key) is None:
            cache[key] = order


def prefetch_existing_items(db: Session, rows, existing_items):
    source_ids = set()
    source_keys = set()
    item_keys = set()
    for row in rows:
        source_id = normalize_text(row.get("source_import_id"))
        if source_id:
            source_ids.add(source_id)
            source_keys.add(source_import_lookup_key(source_id))
        elif row.get("item_key"):
            item_keys.add(row["item_key"])
    existing_items["source_import_id"].update({value: None for value in source_ids})
    existing_items["item_key"].update({value: None for value in item_keys})
    matches = []
    if source_ids:
        matches.append(or_(
            OrderItem.source_import_key.in_(source_keys),
            and_(
                OrderItem.source_import_key.is_(None),
                OrderItem.raw_payload["source_import_id"].as_string().in_(source_ids),
            ),
        ))
    if item_keys:
        matches.append(or_(
            OrderItem.import_item_key.in_(item_keys),
            and_(
                OrderItem.import_item_key.is_(None),
                OrderItem.raw_payload["item_key"].as_string().in_(item_keys),
            ),
        ))
    if not matches:
        return
    statement = (
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .options(selectinload(OrderItem.order))
        .where(active_order_predicate())
        .where(or_(*matches))
        .order_by(OrderItem.created_at.asc(), OrderItem.id.asc())
    )
    for item in db.execute(statement).scalars():
        raw_payload = item.raw_payload or {}
        source_id = normalize_text(item.source_import_id) or normalize_text(raw_payload.get("source_import_id"))
        item_key = normalize_text(item.import_item_key) or normalize_text(raw_payload.get("item_key"))
        if source_id in source_ids and existing_items["source_import_id"].get(source_id) is None:
            existing_items["source_import_id"][source_id] = item
        elif not source_id and item_key in item_keys and existing_items["item_key"].get(item_key) is None:
            existing_items["item_key"][item_key] = item


def is_returned_order(order):
    raw_payload = order.raw_payload or {}
    return (
        normalize_text(order.status).casefold() == STATUS_RETURNED
        or normalize_text(raw_payload.get("return_status")).casefold() in {"returned", "return", "возврат"}
    )


def should_split_from_linked_skladbot_order(db: Session, order, import_source: str) -> bool:
    if order is None:
        return False
    if normalize_text(import_source) != SMARTUP_AUTO_IMPORT_SOURCE:
        return False
    raw_payload = order.raw_payload or {}
    has_linked_payload = bool(
        normalize_text(raw_payload.get("skladbot_request_number"))
        or normalize_text(raw_payload.get("skladbot_request_id"))
    )
    if has_linked_payload:
        return True
    return has_skladbot_create_event(db, order)


def has_skladbot_create_event(db: Session, order) -> bool:
    order_id = normalize_text(getattr(order, "id", ""))
    if not order_id:
        return False
    event_id = db.execute(
        select(PendingEvent.id)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.idempotency_key == skladbot_create_idempotency_key(order_id))
        .limit(1)
    ).scalar_one_or_none()
    return event_id is not None


def resolve_order_key_for_import_row(db: Session, row, import_source: str, order_by_key: dict):
    source_order_key = row["order_key"]
    source_order = order_by_key.get(source_order_key)
    if not should_split_from_linked_skladbot_order(db, source_order, import_source):
        return source_order_key, source_order_key, None
    return source_order_key, linked_skladbot_split_order_key(row), source_order


def linked_skladbot_split_order_key(row) -> str:
    split_source_key = (
        stable_smartup_split_source_key(row.get("source_batch_key"))
        or normalize_text(row.get("source_order_id"))
        or normalize_text(row.get("source_import_id"))
        or normalize_text(row.get("source_file"))
        or normalize_text(row.get("product"))
    )
    digest = stable_hash({
        "reason": LINKED_SKLADBOT_SPLIT_REASON,
        "order_key": row.get("order_key"),
        "split_source_key": split_source_key,
    })
    return f"late-skladbot-split:{digest[:48]}"


def stable_smartup_split_source_key(source_batch_key) -> str:
    text = normalize_text(source_batch_key)
    if text.startswith("smartup:") and ":sha256:" in text:
        return text.split(":sha256:", 1)[0]
    return text


def build_order_raw_payload(
    row,
    import_source: str,
    *,
    order_key: str,
    source_order_key: str,
    split_from_order=None,
) -> dict:
    payload = {
        "order_key": order_key,
        "skladbot_request_number": row["skladbot_request_number"],
        "skladbot_request_id": row["skladbot_request_id"],
        "coordinates": row["coordinates"],
        "source": import_source,
        "source_order_id": row["source_order_id"],
        "source_import_id": row["source_import_id"],
        "source_batch_key": row["source_batch_key"],
    }
    if split_from_order is None:
        return payload

    split_raw = split_from_order.raw_payload or {}
    payload.update({
        "source_order_key": source_order_key,
        "split_reason": LINKED_SKLADBOT_SPLIT_REASON,
        "split_from_order_id": str(split_from_order.id),
        "split_from_skladbot_request_number": normalize_text(split_raw.get("skladbot_request_number")),
        "split_from_skladbot_request_id": normalize_text(split_raw.get("skladbot_request_id")),
        "split_source_batch_key": row["source_batch_key"],
        "split_source_order_id": row["source_order_id"],
    })
    return payload


def find_existing_item_for_row(db: Session, row, existing_items):
    source_import_id = normalize_text(row.get("source_import_id"))
    item_key = normalize_text(row.get("item_key"))
    identity_kind = "source_import_id" if source_import_id else "item_key"
    identity_value = source_import_id or item_key
    if not identity_value:
        return None
    cached = existing_items[identity_kind]
    if identity_value in cached:
        return cached[identity_value]

    if source_import_id:
        legacy_value = OrderItem.raw_payload["source_import_id"].as_string()
        identity_match = or_(
            OrderItem.source_import_key == source_import_lookup_key(source_import_id),
            and_(OrderItem.source_import_key.is_(None), legacy_value == source_import_id),
        )
    else:
        legacy_value = OrderItem.raw_payload["item_key"].as_string()
        identity_match = or_(
            OrderItem.import_item_key == item_key,
            and_(OrderItem.import_item_key.is_(None), legacy_value == item_key),
        )

    item = db.execute(
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .options(selectinload(OrderItem.order))
        .where(active_order_predicate())
        .where(identity_match)
        .order_by(OrderItem.created_at.asc(), OrderItem.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    cached[identity_value] = item
    return item


def row_is_duplicate_in_current_import(row, source_import_ids, item_keys):
    source_import_id = row.get("source_import_id")
    if source_import_id:
        return source_import_id in source_import_ids
    item_key = row.get("item_key")
    return bool(item_key and item_key in item_keys)


def source_import_lookup_key(value):
    value = normalize_text(value)
    return stable_hash({"source_import_id": value}) if value else None


def update_existing_order_address(order, row):
    new_address = normalize_text(row.get("address"))
    if not should_update_existing_order_address(order, row):
        return False

    order.address = new_address
    raw_payload = dict(order.raw_payload or {})
    if row.get("coordinates"):
        raw_payload["coordinates"] = row["coordinates"]
    raw_payload["address_backfilled_at"] = datetime.now().isoformat(timespec="seconds")
    raw_payload["address_backfill_source"] = normalize_text(row.get("source_file")) or "import"
    order.raw_payload = raw_payload
    return True


def should_update_existing_order_address(order, row):
    if order is None:
        return False
    new_address = normalize_text(row.get("address"))
    return is_real_address(new_address) and is_missing_address(order.address)


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
    explicit_line_total = parse_money(first_value(raw_row, LINE_TOTAL_FIELDS))
    line_total = imported_line_total or explicit_line_total or calculated_line_total
    status = normalize_status(first_value(raw_row, STATUS_FIELDS))
    source_order_id = first_value(raw_row, ORDER_ID_FIELDS)
    source_import_id = first_value(raw_row, IMPORT_ID_FIELDS)
    source_file = first_value(raw_row, SOURCE_FILE_FIELDS)
    source_row = first_value(raw_row, SOURCE_ROW_FIELDS)
    source_batch_key = first_value(raw_row, SOURCE_BATCH_FIELDS)
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
        "source_batch_key": normalize_text(source_batch_key),
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
    return not text or text in MISSING_ADDRESS_MARKERS or text.startswith(("координаты", "gps"))


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
    raise ImportRowError("invalid date")


def normalize_status(value):
    text = normalize_text(value).lower()
    if text in {"completed", "done", "closed", "выполнено", "готово", "1", "true", "yes"}:
        return STATUS_COMPLETED
    return STATUS_NOT_COMPLETED


def stable_hash(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
