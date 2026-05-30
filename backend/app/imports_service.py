import hashlib
import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, ImportFile, ImportJob, Order, OrderItem
from .orders_service import STATUS_COMPLETED, STATUS_NOT_COMPLETED
from .schemas import ImportCreate, ImportRead, ImportResult


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


class ImportRowError(Exception):
    pass


def create_import(db: Session, payload: ImportCreate):
    rows_total = len(payload.rows)
    errors = []
    duplicate_rows = 0
    invalid_rows = 0
    orders_created = 0
    items_created = 0

    import_job = ImportJob(
        source=normalize_text(payload.source) or "excel",
        status="created",
        rows_total=rows_total,
        rows_imported=0,
        raw_payload={
            "filename": payload.filename,
            "sha256": normalize_text(payload.sha256).lower(),
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

    order_by_key, item_keys = load_existing_import_keys(db)
    for index, raw_row in enumerate(payload.rows, start=1):
        try:
            row = normalize_import_row(raw_row)
        except ImportRowError as exc:
            invalid_rows += 1
            errors.append(f"row {index}: {exc}")
            continue

        if row["item_key"] in item_keys:
            duplicate_rows += 1
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
                "source_order_id": row["source_order_id"],
                "source_import_id": row["source_import_id"],
                "source_file": row["source_file"],
                "source_row": row["source_row"],
                "block_price": row["block_price"],
                "imported_unit_price": row["imported_unit_price"],
                "imported_line_total": row["imported_line_total"],
                "line_total": row["line_total"],
                "calculated_line_total": row["calculated_line_total"],
                "raw_row": raw_row,
            },
        ))
        item_keys.add(row["item_key"])
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
        "errors": errors,
    }
    db.add(AuditLog(
        action="orders_imported",
        entity_type="import",
        entity_id=str(import_job.id),
        payload=import_job.raw_payload,
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
    )


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
    for order in orders:
        order_key = (order.raw_payload or {}).get("order_key") or order.external_id
        if order_key:
            order_by_key[order_key] = order
        for item in order.items:
            item_key = (item.raw_payload or {}).get("item_key")
            if item_key:
                item_keys.add(item_key)
    return order_by_key, item_keys


def normalize_import_row(raw_row):
    order_date = parse_date_value(first_value(raw_row, ORDER_DATE_FIELDS))
    payment_type = first_value(raw_row, PAYMENT_FIELDS)
    client = first_value(raw_row, CLIENT_FIELDS)
    address = first_value(raw_row, ADDRESS_FIELDS) or "Адрес не указан"
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
        "source_order_id": source_order_id,
        "source_import_id": source_import_id,
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
