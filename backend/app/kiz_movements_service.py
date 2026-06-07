from datetime import datetime, timezone

from sqlalchemy import desc, select
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
