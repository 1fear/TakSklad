import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

from .models import KizCode, KizMovement, ScanCode


MOVEMENT_OUTBOUND = "outbound"
MOVEMENT_RE_OUTBOUND = "re_outbound"
MOVEMENT_RETURN = "return"
MOVEMENT_UNDO = "undo"
MOVEMENT_RESET = "reset"
AVAILABLE_FOR_OUTBOUND_MOVEMENTS = {MOVEMENT_RETURN, MOVEMENT_UNDO, MOVEMENT_RESET}


def normalize_kiz_code(code):
    return str(code or "").strip(" \t\r\n")


def lock_kiz_code_for_transaction(db: Session, code):
    normalized = normalize_kiz_code(code)
    if not normalized:
        return False
    if getattr(getattr(db, "bind", None), "dialect", None) is None:
        return False
    if db.bind.dialect.name != "postgresql":
        return False
    first, second = advisory_lock_keys(normalized)
    db.execute(text("SELECT pg_advisory_xact_lock(:first, :second)"), {"first": first, "second": second})
    return True


def lock_kiz_codes_for_transaction(db: Session, codes):
    normalized_codes = sorted({normalize_kiz_code(code) for code in codes if normalize_kiz_code(code)})
    if not normalized_codes:
        return 0
    if getattr(getattr(db, "bind", None), "dialect", None) is None:
        return 0
    if db.bind.dialect.name != "postgresql":
        return 0
    parameters = {}
    values = []
    for index, code in enumerate(normalized_codes):
        first, second = advisory_lock_keys(code)
        parameters[f"first_{index}"] = first
        parameters[f"second_{index}"] = second
        values.append(f"(:first_{index}, :second_{index})")
    db.execute(text(
        "SELECT pg_advisory_xact_lock(lock_key.first_key, lock_key.second_key) "
        f"FROM (VALUES {', '.join(values)}) AS lock_key(first_key, second_key) "
        "ORDER BY lock_key.first_key, lock_key.second_key"
    ), parameters)
    return len(normalized_codes)


def advisory_lock_keys(value):
    digest = hashlib.sha256(normalize_kiz_code(value).encode("utf-8")).digest()
    return signed_int32(digest[:4]), signed_int32(digest[4:8])


def signed_int32(chunk):
    value = int.from_bytes(chunk, byteorder="big", signed=False)
    if value >= 2**31:
        value -= 2**32
    return value


def ensure_kiz_code(db: Session, code):
    normalized = normalize_kiz_code(code)
    kiz = db.execute(select(KizCode).where(KizCode.code == normalized)).scalar_one_or_none()
    if kiz is not None:
        return kiz
    kiz = KizCode(code=normalized)
    db.add(kiz)
    db.flush()
    return kiz


def latest_kiz_movement(db: Session, code):
    normalized = normalize_kiz_code(code)
    if not normalized:
        return None
    return db.execute(
        select(KizMovement)
        .join(KizCode, KizMovement.kiz_id == KizCode.id)
        .where(KizCode.code == normalized)
        .order_by(desc(KizMovement.occurred_at), desc(KizMovement.id))
        .limit(1)
    ).scalar_one_or_none()


def kiz_is_available_for_outbound(movement):
    return movement is None or movement.movement_type in AVAILABLE_FOR_OUTBOUND_MOVEMENTS


def outbound_movement_type_for(movement):
    if movement is not None and movement.movement_type == MOVEMENT_RETURN:
        return MOVEMENT_RE_OUTBOUND
    return MOVEMENT_OUTBOUND


def record_kiz_movement(
    db: Session,
    *,
    code,
    movement_type,
    order_id=None,
    order_item_id=None,
    scan_code_id=None,
    return_reference="",
    source="backend",
    actor="",
    workstation_id="",
    occurred_at=None,
    raw_payload=None,
):
    normalized = normalize_kiz_code(code)
    if not normalized:
        return None
    kiz = ensure_kiz_code(db, normalized)
    movement = KizMovement(
        kiz_id=kiz.id,
        movement_type=movement_type,
        order_id=order_id,
        order_item_id=order_item_id,
        scan_code_id=scan_code_id,
        return_reference=str(return_reference or "").strip() or None,
        source=str(source or "backend").strip() or "backend",
        actor=str(actor or "").strip() or None,
        workstation_id=str(workstation_id or "").strip() or None,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        raw_payload=dict(raw_payload or {}),
    )
    db.add(movement)
    db.flush()
    return movement


def record_kiz_movements(db: Session, records):
    prepared = []
    for record in records:
        normalized = normalize_kiz_code(record.get("code"))
        if normalized:
            prepared.append((normalized, record))
    if not prepared:
        return []

    codes = sorted({code for code, _record in prepared})
    existing = {
        row.code: row
        for row in db.execute(select(KizCode).where(KizCode.code.in_(codes))).scalars()
    }
    missing = []
    for code in codes:
        if code in existing:
            continue
        kiz = KizCode(id=uuid.uuid4(), code=code)
        existing[code] = kiz
        missing.append(kiz)
    if missing:
        db.add_all(missing)

    movements = []
    for code, record in prepared:
        movement = KizMovement(
            id=uuid.uuid4(),
            kiz_id=existing[code].id,
            movement_type=record["movement_type"],
            order_id=record.get("order_id"),
            order_item_id=record.get("order_item_id"),
            scan_code_id=record.get("scan_code_id"),
            return_reference=str(record.get("return_reference") or "").strip() or None,
            source=str(record.get("source") or "backend").strip() or "backend",
            actor=str(record.get("actor") or "").strip() or None,
            workstation_id=str(record.get("workstation_id") or "").strip() or None,
            occurred_at=record.get("occurred_at") or datetime.now(timezone.utc),
            raw_payload=dict(record.get("raw_payload") or {}),
        )
        movements.append(movement)
    db.add_all(movements)
    return movements


def find_same_item_scan(db: Session, *, code, order_item_id):
    return db.execute(
        select(ScanCode)
        .where(ScanCode.code == normalize_kiz_code(code))
        .where(ScanCode.order_item_id == order_item_id)
        .order_by(desc(ScanCode.scanned_at), desc(ScanCode.id))
        .limit(1)
    ).scalar_one_or_none()


def find_other_item_scan(db: Session, *, code, order_item_id):
    return db.execute(
        select(ScanCode)
        .where(ScanCode.code == normalize_kiz_code(code))
        .where(ScanCode.order_item_id != order_item_id)
        .order_by(desc(ScanCode.scanned_at), desc(ScanCode.id))
        .limit(1)
    ).scalar_one_or_none()
