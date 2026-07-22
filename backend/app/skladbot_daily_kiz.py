"""Read-only KIZ hydration for the combined SkladBot daily workbook."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, desc, exists, func, or_, select
from sqlalchemy.orm import aliased, selectinload

from .kiz_movements_service import (
    MOVEMENT_OUTBOUND,
    MOVEMENT_RE_OUTBOUND,
    normalize_kiz_code,
)
from .models import KizCode, KizMovement, Order, OrderItem, ScanCode
from .reports_service import (
    parse_datetime_value,
    payment_group,
    report_timezone,
    scan_business_date,
    scan_business_date_expression,
    sql_date_value,
)
from .scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    SCAN_TYPE_UNIT,
    scan_block_quantity,
    scan_type_for_code,
)
from .skladbot_contracts import (
    canonical_remote_request_id,
    canonical_skladbot_request_evidence_link,
    canonical_skladbot_request_number,
    normalize_text,
    parse_date,
)


MARKING_CODE_HEADERS = [
    "Номер",
    "ID заявки",
    "Smartup ID",
    "Дата выгрузки",
    "Тип оплаты",
    "Товар",
    "КИЗ",
    "Время скана",
    "Тип скана",
    "Блоков по коду",
]

ACTIVE_KIZ_MOVEMENT_TYPES = {MOVEMENT_OUTBOUND, MOVEMENT_RE_OUTBOUND}
KNOWN_KIZ_MOVEMENT_TYPES = {
    MOVEMENT_OUTBOUND,
    MOVEMENT_RE_OUTBOUND,
    "return",
    "undo",
    "reset",
}
VALID_SCAN_TYPES = {SCAN_TYPE_UNIT, SCAN_TYPE_AGGREGATE_BOX}


class DailyKizHydrationError(RuntimeError):
    """Fail-closed error with a fixed message that never contains a KIZ."""


def enrich_daily_kiz_from_orders(db, report: dict[str, Any]) -> None:
    """Attach exact request KIZ rows and active terminal KIZ rows to ``report``.

    The function is read-only. It deliberately serializes ORM objects into
    plain dictionaries while the caller-owned session is open.
    """
    requests = [row for row in report.get("requests") or [] if isinstance(row, dict)]
    report["request_kiz_rows"] = []
    report["daily_kiz_rows"] = []
    for request in requests:
        request["kiz_count"] = None
        request["kiz_codes"] = None

    report_date = _report_date(report.get("report_date"))
    request_pairs = set()
    for request in requests:
        pair = _request_evidence_pair(request)
        if all(pair):
            request_pairs.add(pair)
    owners = _load_candidate_owners(db, request_pairs)
    exact_owners = _exact_owner_by_pair(owners, request_pairs)
    owner_pairs: dict[str, set[tuple[str, str]]] = {}
    for pair, owner in exact_owners.items():
        owner_pairs.setdefault(str(owner.id), set()).add(pair)
    ambiguous_owner_ids = {
        owner_id
        for owner_id, pairs in owner_pairs.items()
        if len(pairs) > 1
    }
    exact_owners = {
        pair: owner
        for pair, owner in exact_owners.items()
        if str(owner.id) not in ambiguous_owner_ids
    }

    day_seed_rows = _load_scan_rows_for_business_date(db, report_date)
    request_seed_scans = [
        scan
        for owner in exact_owners.values()
        for item in owner.items
        for scan in item.scan_codes
    ]
    seed_codes = {
        normalized
        for scan in [*request_seed_scans, *(row[0] for row in day_seed_rows)]
        if (normalized := normalize_kiz_code(scan.code))
    }
    active_rows = _load_active_scan_rows(db, seed_codes)

    active_by_order: dict[str, list[tuple[ScanCode, OrderItem, Order]]] = {}
    for scan, item, owner in active_rows:
        active_by_order.setdefault(str(owner.id), []).append((scan, item, owner))

    request_rows: list[dict[str, Any]] = []
    request_metadata_by_owner: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        pair = _request_evidence_pair(request)
        owner = exact_owners.get(pair)
        if owner is None:
            continue
        metadata = _request_metadata(request, owner)
        request_metadata_by_owner.setdefault(str(owner.id), []).append(metadata)
        rows = sorted(
            active_by_order.get(str(owner.id), []),
            key=_scan_row_sort_key,
        )
        request["kiz_count"] = len(rows)
        request["kiz_codes"] = [scan.code for scan, _item, _owner in rows]
        request_rows.extend(
            _serialize_scan_row(scan, item, owner, request_metadata=metadata)
            for scan, item, owner in rows
        )

    daily_rows = []
    for scan, item, owner in active_rows:
        if payment_group(owner.payment_type) != "terminal" or scan_business_date(scan) != report_date:
            continue
        owner_metadata = request_metadata_by_owner.get(str(owner.id), [])
        metadata = owner_metadata[0] if len(owner_metadata) == 1 else {}
        daily_rows.append(_serialize_scan_row(scan, item, owner, request_metadata=metadata))
    report["request_kiz_rows"] = sorted(request_rows, key=_serialized_request_row_sort_key)
    report["daily_kiz_rows"] = sorted(daily_rows, key=_serialized_daily_row_sort_key)


def _report_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = parse_date(value)
    if parsed is None:
        raise DailyKizHydrationError("daily_kiz_report_date_invalid")
    return parsed


def _request_evidence_pair(request: dict[str, Any]) -> tuple[str, str]:
    return canonical_skladbot_request_evidence_link(
        request,
        allow_missing_raw=True,
        allow_single_raw_side=True,
    )


def _load_candidate_owners(db, request_pairs: set[tuple[str, str]]) -> list[Order]:
    if not request_pairs:
        return []
    report_ids = sorted({request_id for request_id, _number in request_pairs})
    report_numbers = sorted({number for _request_id, number in request_pairs})
    candidate_item = aliased(OrderItem)
    order_raw = Order.raw_payload
    item_raw = candidate_item.raw_payload
    order_matches = or_(
        *(func.trim(func.coalesce(order_raw[key].as_string(), "")).in_(report_ids)
          for key in ("skladbot_request_id", "skladbot_return_request_id")),
        *(func.trim(func.coalesce(order_raw[key].as_string(), "")).in_(report_numbers)
          for key in ("skladbot_request_number", "skladbot_return_request_number")),
    )
    item_matches = or_(
        *(func.trim(func.coalesce(item_raw[key].as_string(), "")).in_(report_ids)
          for key in ("skladbot_request_id", "skladbot_return_request_id")),
        *(func.trim(func.coalesce(item_raw[key].as_string(), "")).in_(report_numbers)
          for key in ("skladbot_request_number", "skladbot_return_request_number")),
    )
    owner_ids = select(Order.id).where(or_(
        order_matches,
        exists(select(1).where(candidate_item.order_id == Order.id, item_matches)),
    ))
    return list(db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id.in_(owner_ids))
    ).scalars().unique())


def _exact_owner_by_pair(
    owners: list[Order],
    request_pairs: set[tuple[str, str]],
) -> dict[tuple[str, str], Order]:
    owners_by_id = {str(owner.id): owner for owner in owners}
    pair_owner_ids: dict[tuple[str, str], set[str]] = {}
    id_links: dict[str, set[tuple[str, str]]] = {}
    number_links: dict[str, set[tuple[str, str]]] = {}
    for owner in owners:
        owner_id = str(owner.id)
        payloads = [owner.raw_payload or {}, *((item.raw_payload or {}) for item in owner.items)]
        for payload in payloads:
            for prefix in ("skladbot_request", "skladbot_return_request"):
                request_id = canonical_remote_request_id(payload.get(f"{prefix}_id"))
                number = canonical_skladbot_request_number(payload.get(f"{prefix}_number"))
                link = (request_id, number)
                if request_id:
                    id_links.setdefault(request_id, set()).add(link)
                if number:
                    number_links.setdefault(number, set()).add(link)
                if request_id and number:
                    pair_owner_ids.setdefault(link, set()).add(owner_id)

    exact: dict[tuple[str, str], Order] = {}
    for pair in request_pairs:
        owner_ids = pair_owner_ids.get(pair, set())
        if (
            len(owner_ids) == 1
            and id_links.get(pair[0], set()) == {pair}
            and number_links.get(pair[1], set()) == {pair}
        ):
            exact[pair] = owners_by_id[next(iter(owner_ids))]
    return exact


def _load_scan_rows_for_business_date(db, report_date: date):
    scan_day = scan_business_date_expression(db, ScanCode.raw_payload, ScanCode.scanned_at)
    return db.execute(
        select(ScanCode, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ScanCode.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(scan_day == sql_date_value(db, report_date))
    ).all()


def _load_active_scan_rows(db, normalized_codes: set[str]):
    if not normalized_codes:
        return []
    scan_rows = db.execute(
        select(ScanCode, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ScanCode.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(func.trim(ScanCode.code).in_(sorted(normalized_codes)))
    ).all()
    latest_by_code = _load_latest_movements(db, normalized_codes)
    active_by_code: dict[str, list[tuple[ScanCode, OrderItem, Order]]] = {}
    for scan, item, owner in scan_rows:
        code = normalize_kiz_code(scan.code)
        latest = latest_by_code.get(code)
        if latest is None:
            active_by_code.setdefault(code, []).append((scan, item, owner))
            continue
        movement_type = normalize_text(latest["movement_type"])
        if movement_type not in KNOWN_KIZ_MOVEMENT_TYPES:
            raise DailyKizHydrationError("daily_kiz_lifecycle_unknown")
        if movement_type not in ACTIVE_KIZ_MOVEMENT_TYPES:
            continue
        if str(latest["scan_code_id"] or "") != str(scan.id):
            continue
        if str(latest["order_item_id"] or "") != str(item.id):
            raise DailyKizHydrationError("daily_kiz_lifecycle_owner_mismatch")
        active_by_code.setdefault(code, []).append((scan, item, owner))

    unresolved_busy_codes = {
        code
        for code, latest in latest_by_code.items()
        if normalize_text(latest["movement_type"]) in ACTIVE_KIZ_MOVEMENT_TYPES
        and code not in active_by_code
    }
    if unresolved_busy_codes:
        raise DailyKizHydrationError("daily_kiz_active_scan_missing")

    result = []
    for rows in active_by_code.values():
        if len(rows) > 1:
            raise DailyKizHydrationError("daily_kiz_duplicate_active_code")
        result.append(rows[0])
    return sorted(result, key=_scan_row_sort_key)


def _load_latest_movements(db, normalized_codes: set[str]) -> dict[str, dict[str, Any]]:
    ranked = select(
        KizMovement.kiz_id.label("kiz_id"),
        KizMovement.movement_type.label("movement_type"),
        KizMovement.scan_code_id.label("scan_code_id"),
        KizMovement.order_item_id.label("order_item_id"),
        func.row_number().over(
            partition_by=KizMovement.kiz_id,
            order_by=(desc(KizMovement.occurred_at), desc(KizMovement.id)),
        ).label("movement_rank"),
    ).subquery("daily_kiz_latest_movement_ranked")
    rows = db.execute(
        select(
            KizCode.code,
            ranked.c.movement_type,
            ranked.c.scan_code_id,
            ranked.c.order_item_id,
        )
        .join(ranked, and_(ranked.c.kiz_id == KizCode.id, ranked.c.movement_rank == 1))
        .where(KizCode.code.in_(sorted(normalized_codes)))
    ).all()
    return {
        normalize_kiz_code(code): {
            "movement_type": movement_type,
            "scan_code_id": scan_code_id,
            "order_item_id": order_item_id,
        }
        for code, movement_type, scan_code_id, order_item_id in rows
    }


def _serialize_scan_row(
    scan: ScanCode,
    item: OrderItem,
    owner: Order,
    *,
    request_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_metadata = request_metadata or {}
    return {
        "request_number": request_metadata.get("request_number") or "",
        "request_id": request_metadata.get("request_id") or "",
        "smartup_id": request_metadata.get("smartup_id") or "",
        "unloading_date": request_metadata.get("unloading_date") or "",
        "payment_type": owner.payment_type or "",
        "product": item.product or "",
        "code": scan.code,
        "scan_type": _scan_type(scan),
        "block_quantity": scan_block_quantity(scan),
        "scanned_at": _scan_timestamp_text(scan),
    }


def _request_metadata(request: dict[str, Any], owner: Order) -> dict[str, Any]:
    request_id, request_number = _request_evidence_pair(request)
    return {
        "request_number": request_number,
        "request_id": request_id,
        "smartup_id": normalize_text(request.get("smartup_id")),
        "unloading_date": normalize_text(request.get("unloading_date")),
        "payment_type": owner.payment_type or "",
    }


def _scan_type(scan: ScanCode) -> str:
    stored = normalize_text((scan.raw_payload or {}).get("scan_type"))
    return stored if stored in VALID_SCAN_TYPES else scan_type_for_code(scan.code)


def _scan_timestamp_text(scan: ScanCode) -> str:
    raw_value = (scan.raw_payload or {}).get("scanned_at")
    parsed_raw = parse_datetime_value(raw_value)
    if parsed_raw is not None:
        if parsed_raw.tzinfo is not None:
            return parsed_raw.astimezone(report_timezone()).isoformat()
        return parsed_raw.isoformat()
    fallback = scan.scanned_at
    if isinstance(fallback, datetime):
        if fallback.tzinfo is not None:
            fallback = fallback.astimezone(report_timezone())
        return fallback.isoformat()
    return normalize_text(fallback)


def _scan_row_sort_key(row) -> tuple[str, str, str, str]:
    scan, item, owner = row
    return (
        _scan_timestamp_text(scan),
        normalize_kiz_code(scan.code),
        normalize_text(item.product),
        str(owner.id),
    )


def _serialized_request_row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_text(row.get("request_number")),
        normalize_text(row.get("product")),
        normalize_kiz_code(row.get("code")),
    )


def _serialized_daily_row_sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        normalize_text(row.get("scanned_at")),
        normalize_text(row.get("request_number")),
        normalize_text(row.get("product")),
        normalize_kiz_code(row.get("code")),
    )
