import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import case, desc, func, select, text
from sqlalchemy.orm import Session

from .models import KizCode, KizMovement, ScanCode


MOVEMENT_OUTBOUND = "outbound"
MOVEMENT_RE_OUTBOUND = "re_outbound"
MOVEMENT_RETURN = "return"
MOVEMENT_UNDO = "undo"
MOVEMENT_RESET = "reset"
AVAILABLE_FOR_OUTBOUND_MOVEMENTS = {MOVEMENT_RETURN, MOVEMENT_UNDO, MOVEMENT_RESET}
_KIZ_CODE_NOT_PROVIDED = object()


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


def lookup_kiz_state(db: Session, code):
    """Load the code row and its latest movement in one indexed round trip."""

    normalized = normalize_kiz_code(code)
    if not normalized:
        return None, None
    row = db.execute(
        select(KizCode, KizMovement)
        .outerjoin(KizMovement, KizMovement.kiz_id == KizCode.id)
        .where(KizCode.code == normalized)
        .order_by(desc(KizMovement.occurred_at), desc(KizMovement.id))
        .limit(1)
    ).first()
    return row if row is not None else (None, None)


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
    kiz_code=_KIZ_CODE_NOT_PROVIDED,
):
    normalized = normalize_kiz_code(code)
    if not normalized:
        return None
    if kiz_code is _KIZ_CODE_NOT_PROVIDED:
        kiz = ensure_kiz_code(db, normalized)
    elif kiz_code is None:
        kiz = KizCode(code=normalized)
        db.add(kiz)
        db.flush()
    else:
        kiz = kiz_code
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


def locked_existing_kiz_ids(db: Session, codes):
    """Acquire ordered PostgreSQL advisory locks and load KIZ ids in one trip."""

    codes = sorted({normalize_kiz_code(code) for code in codes if normalize_kiz_code(code)})
    if not codes:
        return {}
    if db.bind.dialect.name != "postgresql":
        return {
            code: kiz_id
            for code, kiz_id in db.execute(
                select(KizCode.code, KizCode.id).where(KizCode.code.in_(codes))
            ).all()
        }

    parameters = {}
    values = []
    for index, code in enumerate(codes):
        first, second = advisory_lock_keys(code)
        parameters.update({
            f"code_{index}": code,
            f"first_{index}": first,
            f"second_{index}": second,
        })
        values.append(f"(:code_{index}, :first_{index}, :second_{index})")
    rows = db.execute(text(
        "SELECT lock_key.code, kiz_codes.id, "
        "pg_advisory_xact_lock(lock_key.first_key, lock_key.second_key) AS lock_acquired "
        f"FROM (VALUES {', '.join(values)}) AS lock_key(code, first_key, second_key) "
        "LEFT JOIN kiz_codes ON kiz_codes.code = lock_key.code "
        "ORDER BY lock_key.first_key, lock_key.second_key"
    ), parameters).all()
    return {str(code): kiz_id for code, kiz_id, _lock_acquired in rows if kiz_id is not None}


def record_kiz_movements(db: Session, records, *, lock_codes=False):
    prepared = []
    for record in records:
        normalized = normalize_kiz_code(record.get("code"))
        if normalized:
            prepared.append((normalized, record))
    if not prepared:
        return []

    codes = sorted({code for code, _record in prepared})
    if lock_codes:
        existing_ids = locked_existing_kiz_ids(db, codes)
    else:
        existing_ids = {
            code: kiz_id
            for code, kiz_id in db.execute(
                select(KizCode.code, KizCode.id).where(KizCode.code.in_(codes))
            ).all()
        }
    missing = []
    for code in codes:
        if code in existing_ids:
            continue
        kiz = KizCode(id=uuid.uuid4(), code=code)
        existing_ids[code] = kiz.id
        missing.append(kiz)
    if missing:
        db.add_all(missing)

    movements = []
    for code, record in prepared:
        movement = KizMovement(
            id=uuid.uuid4(),
            kiz_id=existing_ids[code],
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


def find_item_scans(db: Session, *, code, order_item_id):
    """Return latest same-item and other-item scans with one database query."""

    match_kind = case(
        (ScanCode.order_item_id == order_item_id, "same"),
        else_="other",
    )
    ranked = (
        select(
            ScanCode.id.label("scan_id"),
            match_kind.label("match_kind"),
            func.row_number().over(
                partition_by=match_kind,
                order_by=(desc(ScanCode.scanned_at), desc(ScanCode.id)),
            ).label("match_rank"),
        )
        .where(ScanCode.code == normalize_kiz_code(code))
        .subquery()
    )
    rows = db.execute(
        select(ScanCode, ranked.c.match_kind)
        .join(ranked, ranked.c.scan_id == ScanCode.id)
        .where(ranked.c.match_rank == 1)
    ).all()
    by_kind = {match: scan for scan, match in rows}
    return by_kind.get("same"), by_kind.get("other")
