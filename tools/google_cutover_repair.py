#!/usr/bin/env python3
"""Fail-closed one-shot repair for legacy Google return lifecycle gaps.

This file is streamed into the v2.0.39 backend for read-only planning and into
an isolated one-shot container for apply.  Its stdout is counts-only:
identifiers and KIZ values stay on the VDS.  Apply mode is a single transaction
protected by a plan hash, global advisory lock, sorted KIZ locks, deterministic
UUIDs and exact expected audit counts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo


APPROVAL = "GOOGLE_CUTOVER_RETURN_REPAIR_APPROVED"
NAMESPACE = uuid.UUID("b9080367-802f-5ca4-8673-dfa7adb7a846")
RETURN_MARKERS = {"возврат", "returned", "return"}
OUTBOUND_MOVEMENTS = {"outbound", "re_outbound"}
AVAILABLE_MOVEMENTS = {"return", "undo", "reset"}
IDENTITY_DIAGNOSTIC_FIELDS = (
    "identity_no_strong_id_records",
    "identity_not_found_records",
    "identity_product_quantity_mismatch_records",
    "identity_multiple_records",
    "identity_order_not_returned_records",
)
AMBIGUOUS_ERRORS = {
    "target_missing_movement_timestamp",
    "target_return_crosses_later_movement",
    "missing_scan_return_boundary_conflict",
}
OTHER_ERRORS = {
    "multiple_item_scans",
    "missing_outbound",
    "outbound_owner_mismatch",
    "busy_before_missing_scan",
    "scan_without_outbound",
    "scanned_blocks_exceed_plan",
    "return_reference_too_long",
    "returned_by_too_long",
}


def backend_module(name):
    """Import from the legacy container or from the repository test layout."""

    try:
        return importlib.import_module(f"app.{name}")
    except ModuleNotFoundError as exc:
        if exc.name not in {"app", f"app.{name}"}:
            raise
        return importlib.import_module(f"backend.app.{name}")


def normalize(value):
    """KIZ-safe normalization: trim edge ASCII whitespace only."""

    return str(value or "").strip(" \t\r\n")


def aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def parse_returned_at(order, record):
    payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    values = (
        (payload.get("returned_at"), "backend_order"),
        (record.get("returned_at"), "google_sheet"),
    )
    tashkent = ZoneInfo("Asia/Tashkent")
    for raw_value, provenance in values:
        text = normalize(raw_value)
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc), provenance
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=tashkent).astimezone(timezone.utc), provenance
            except ValueError:
                pass
    return None, ""


def is_returned_record(record):
    status = normalize(record.get("return_status")).casefold()
    return status in RETURN_MARKERS or "возврат" in status


def order_is_returned(order):
    payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    return order.status == "returned" or normalize(payload.get("return_status")).casefold() == "returned"


def product_quantity_match(item, record):
    product = normalize(record.get("product")).casefold()
    if product and normalize(item.product).casefold() != product:
        return False
    blocks = int(record.get("quantity_blocks") or 0)
    pieces = int(record.get("quantity_pieces") or 0)
    if blocks > 0 and int(item.quantity_blocks or 0) != blocks:
        return False
    if pieces > 0 and int(item.quantity_pieces or 0) != pieces:
        return False
    return True


def match_records_to_items(db, records):
    """Use unique strong identities; never inherit legacy ``setdefault`` ambiguity."""

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload, selectinload

    OrderItem = backend_module("models").OrderItem

    items = db.execute(
        select(OrderItem)
        .options(joinedload(OrderItem.order), selectinload(OrderItem.scan_codes))
        .order_by(OrderItem.created_at, OrderItem.id)
    ).scalars().all()
    by_import = defaultdict(list)
    by_order = defaultdict(list)
    for item in items:
        payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
        source_import_id = normalize(item.source_import_id) or normalize(payload.get("source_import_id"))
        source_order_id = normalize(payload.get("source_order_id"))
        if source_import_id:
            by_import[source_import_id].append(item)
        if source_order_id:
            by_order[source_order_id].append(item)

    matched = []
    identity_diagnostics = {field: 0 for field in IDENTITY_DIAGNOSTIC_FIELDS}
    for record in records:
        source_import_id = normalize(record.get("source_import_id"))
        source_order_id = normalize(record.get("source_order_id"))
        if source_import_id:
            pool = by_import.get(source_import_id, [])
            candidates = [
                item for item in pool
                if product_quantity_match(item, record)
            ]
        elif source_order_id:
            pool = by_order.get(source_order_id, [])
            candidates = [
                item for item in pool
                if product_quantity_match(item, record)
            ]
        else:
            pool = []
            candidates = []
        item = candidates[0] if len(candidates) == 1 else None
        if is_returned_record(record) and len(candidates) != 1:
            if not source_import_id and not source_order_id:
                field = "identity_no_strong_id_records"
            elif not pool:
                field = "identity_not_found_records"
            elif not candidates:
                field = "identity_product_quantity_mismatch_records"
            else:
                field = "identity_multiple_records"
            identity_diagnostics[field] += 1
        matched.append((record, item))
    return matched, identity_diagnostics


def load_records_and_items(db):
    records = backend_module("google_sheets_sync_worker").load_google_sheet_records()
    return match_records_to_items(db, records)


def load_runtime_state(db, records_and_items):
    from sqlalchemy import select

    models = backend_module("models")
    KizCode, KizMovement, ScanCode = models.KizCode, models.KizMovement, models.ScanCode

    codes = sorted({
        normalize(code)
        for record, _item in records_and_items
        if is_returned_record(record)
        for code in (record.get("scanned_codes") or [])
        if normalize(code)
    })
    scans_by_code = defaultdict(list)
    movements_by_code = defaultdict(list)
    if codes:
        for scan in db.execute(
            select(ScanCode)
            .where(ScanCode.code.in_(codes))
            .order_by(ScanCode.scanned_at, ScanCode.id)
        ).scalars():
            scans_by_code[normalize(scan.code)].append(scan)
        for code, movement in db.execute(
            select(KizCode.code, KizMovement)
            .join(KizMovement, KizMovement.kiz_id == KizCode.id)
            .where(KizCode.code.in_(codes))
            .order_by(KizCode.code, KizMovement.occurred_at, KizMovement.id)
        ).all():
            movements_by_code[normalize(code)].append(movement)
    return scans_by_code, movements_by_code


def movement_time(movement):
    return aware_utc(movement.occurred_at)


def movement_for_scan(movements, scan, movement_types):
    matches = movements_for_scan(movements, scan, movement_types)
    return matches[-1] if matches else None


def movements_for_scan(movements, scan, movement_types):
    return [
        movement for movement in movements
        if str(movement.scan_code_id or "") == str(scan.id)
        and normalize(movement.movement_type) in movement_types
    ]


def movement_matches_item(movement, item):
    return (
        str(movement.order_id or "") == str(item.order.id)
        and str(movement.order_item_id or "") == str(item.id)
    )


def candidate_payload(candidate):
    return {
        "code": candidate["code"],
        "order_id": str(candidate["item"].order.id),
        "item_id": str(candidate["item"].id),
        "kind": candidate["kind"],
        "outbound_type": candidate["outbound_type"],
        "scan_id": str(candidate["scan"].id) if candidate.get("scan") is not None else "",
        "new_scan_id": str(deterministic_uuid("scan", candidate["item"].id, candidate["code"])),
        "outbound_id": str(deterministic_uuid(
            "movement",
            candidate["scan"].id if candidate.get("scan") is not None else deterministic_uuid(
                "scan", candidate["item"].id, candidate["code"]
            ),
            candidate["outbound_type"],
        )),
        "return_id": str(deterministic_uuid(
            "movement",
            candidate["scan"].id if candidate.get("scan") is not None else deterministic_uuid(
                "scan", candidate["item"].id, candidate["code"]
            ),
            "return",
        )),
        "scan_at": candidate["scan_at"].isoformat(),
        "return_at": candidate["return_at"].isoformat(),
        "original_return_at": candidate["original_return_at"].isoformat(),
        "timestamp_provenance": candidate["timestamp_provenance"],
        "timestamp_adjusted": bool(candidate["timestamp_adjusted"]),
        "new_scanned_blocks": int(candidate["new_scanned_blocks"]),
        "return_reference": normalize(candidate["record"].get("return_reference")),
        "returned_by": normalize(candidate["record"].get("returned_by")),
        "source_import_id": normalize(candidate["record"].get("source_import_id")),
        "source_order_id": normalize(candidate["record"].get("source_order_id")),
        "source_sheet": normalize(candidate["record"].get("source_sheet")),
        "row_number": int(candidate["record"].get("row_number") or 0),
    }


def equivalent_candidate_payload(candidate):
    payload = candidate_payload(candidate)
    payload.pop("source_sheet")
    payload.pop("row_number")
    return payload


def plan_hash(candidates):
    canonical = [candidate_payload(candidate) for candidate in candidates]
    encoded = json.dumps(sorted(canonical, key=lambda row: tuple(row.values())), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_target(item, record, code, scans_by_code, movements_by_code):
    scan_quantities = backend_module("scan_quantities")
    scan_metadata_for_code = scan_quantities.scan_metadata_for_code
    scanned_blocks_for_scans = scan_quantities.scanned_blocks_for_scans

    return_at, timestamp_provenance = parse_returned_at(item.order, record)
    if return_at is None:
        return None, "unparseable_returned_at"
    if len(normalize(record.get("return_reference"))) > 120:
        return None, "return_reference_too_long"
    if len(normalize(record.get("returned_by"))) > 120:
        return None, "returned_by_too_long"
    movements = list(movements_by_code.get(code) or [])
    item_scans = [scan for scan in (item.scan_codes or []) if normalize(scan.code) == code]
    if len(item_scans) > 1:
        return None, "multiple_item_scans"

    if item_scans:
        scan = item_scans[0]
        outbound_movements = movements_for_scan(movements, scan, OUTBOUND_MOVEMENTS)
        return_movements = movements_for_scan(movements, scan, {"return"})
        if return_movements:
            anomaly = False
            if any(movement_time(movement) is None for movement in movements):
                anomaly = True
            elif len(outbound_movements) != 1 or len(return_movements) != 1:
                anomaly = True
            else:
                outbound = outbound_movements[0]
                existing_return = return_movements[0]
                outbound_at = movement_time(outbound)
                existing_return_at = movement_time(existing_return)
                if (
                    not movement_matches_item(outbound, item)
                    or not movement_matches_item(existing_return, item)
                    or existing_return_at <= outbound_at
                ):
                    anomaly = True
                else:
                    between = [
                        movement for movement in movements
                        if movement.id not in {outbound.id, existing_return.id}
                        and outbound_at <= movement_time(movement) <= existing_return_at
                    ]
                    anomaly = bool(between)
            return {
                "kind": "already_repaired",
                "preexisting_anomaly": anomaly,
            }, ""
        if any(movement_time(movement) is None for movement in movements):
            return None, "target_missing_movement_timestamp"
        if len(outbound_movements) != 1:
            return None, "missing_outbound"
        outbound = outbound_movements[0]
        if not movement_matches_item(outbound, item):
            return None, "outbound_owner_mismatch"
        outbound_at = movement_time(outbound)
        candidate_return_at = return_at
        adjusted = False
        if candidate_return_at <= outbound_at:
            candidate_return_at = outbound_at + timedelta(microseconds=1)
            adjusted = True
        later = [
            movement for movement in movements
            if movement.id != outbound.id and movement_time(movement) >= outbound_at
        ]
        if later and candidate_return_at >= movement_time(later[0]):
            return None, "target_return_crosses_later_movement"
        return {
            "kind": "missing_return",
            "code": code,
            "record": record,
            "item": item,
            "scan": scan,
            "outbound_type": normalize(outbound.movement_type),
            "scan_at": outbound_at,
            "return_at": candidate_return_at,
            "original_return_at": return_at,
            "timestamp_provenance": timestamp_provenance,
            "timestamp_adjusted": adjusted,
            "new_scanned_blocks": int(item.scanned_blocks or 0),
        }, ""

    # Missing historical scan. Existing lifecycle is allowed only when it is
    # chronologically compatible: the code was free before this return and any
    # later outbound remains later than the inserted return.
    if any(movement_time(movement) is None for movement in movements):
        return None, "target_missing_movement_timestamp"
    before = [movement for movement in movements if movement_time(movement) < return_at]
    after = [movement for movement in movements if movement_time(movement) >= return_at]
    previous = before[-1] if before else None
    if previous is not None and normalize(previous.movement_type) not in AVAILABLE_MOVEMENTS:
        return None, "busy_before_missing_scan"
    for scan in scans_by_code.get(code) or []:
        outbound = movement_for_scan(movements, scan, OUTBOUND_MOVEMENTS)
        if outbound is None:
            return None, "scan_without_outbound"
    if after and movement_time(after[0]) <= return_at:
        return None, "missing_scan_return_boundary_conflict"

    scan_at = return_at - timedelta(microseconds=1)
    if previous is not None and movement_time(previous) >= scan_at:
        return None, "missing_scan_return_boundary_conflict"
    outbound_type = "re_outbound" if previous is not None and normalize(previous.movement_type) == "return" else "outbound"
    metadata = scan_metadata_for_code(code)
    synthetic_scan = SimpleNamespace(code=code, raw_payload=metadata)
    computed_blocks = scanned_blocks_for_scans([*(item.scan_codes or []), synthetic_scan])
    new_scanned_blocks = max(int(item.scanned_blocks or 0), int(computed_blocks or 0))
    if int(item.quantity_blocks or 0) > 0 and new_scanned_blocks > int(item.quantity_blocks or 0):
        return None, "scanned_blocks_exceed_plan"
    return {
        "kind": "missing_scan",
        "code": code,
        "record": record,
        "item": item,
        "scan": None,
        "outbound_type": outbound_type,
        "scan_at": scan_at,
        "return_at": return_at,
        "original_return_at": return_at,
        "timestamp_provenance": timestamp_provenance,
        "timestamp_adjusted": False,
        "new_scanned_blocks": new_scanned_blocks,
    }, ""


def build_repair_plan(
    records_and_items,
    scans_by_code,
    movements_by_code,
    *,
    identity_conflicts=0,
    identity_diagnostics=None,
):
    identity_diagnostics = dict(identity_diagnostics or {})
    counts = {
        "returned_code_occurrences": 0,
        "already_repaired_occurrences": 0,
        "missing_scan_occurrences": 0,
        "missing_return_occurrences": 0,
        "duplicate_occurrences": 0,
        "identity_conflicts": int(identity_conflicts),
        "unparseable_returned_at": 0,
        "ambiguous_chronology": 0,
        "other_conflicts": 0,
        "preexisting_anomaly_occurrences": 0,
        "code_owner_conflicts": 0,
        "divergent_duplicate_targets": 0,
        **{field: int(identity_diagnostics.get(field) or 0) for field in IDENTITY_DIAGNOSTIC_FIELDS},
        **{f"{error}_occurrences": 0 for error in sorted(AMBIGUOUS_ERRORS | OTHER_ERRORS)},
    }
    candidates_by_target = {}
    code_owner = {}
    for record, item in records_and_items:
        if not is_returned_record(record):
            continue
        if item is None or not order_is_returned(item.order):
            if item is not None:
                counts["identity_conflicts"] += 1
                counts["identity_order_not_returned_records"] += 1
            continue
        for code in dict.fromkeys(
            normalize(value)
            for value in (record.get("scanned_codes") or [])
            if normalize(value)
        ):
            counts["returned_code_occurrences"] += 1
            owner = code_owner.setdefault(code, str(item.id))
            if owner != str(item.id):
                counts["other_conflicts"] += 1
                counts["code_owner_conflicts"] += 1
                continue
            target_key = (str(item.id), code)
            candidate, error = classify_target(item, record, code, scans_by_code, movements_by_code)
            if candidate is not None and candidate.get("kind") == "already_repaired":
                counts["already_repaired_occurrences"] += 1
                counts["preexisting_anomaly_occurrences"] += int(
                    bool(candidate.get("preexisting_anomaly"))
                )
                continue
            if error:
                if error == "unparseable_returned_at":
                    counts["unparseable_returned_at"] += 1
                elif error in AMBIGUOUS_ERRORS:
                    counts["ambiguous_chronology"] += 1
                    counts[f"{error}_occurrences"] += 1
                else:
                    counts["other_conflicts"] += 1
                    if error in OTHER_ERRORS:
                        counts[f"{error}_occurrences"] += 1
                continue
            field = f"{candidate['kind']}_occurrences"
            counts[field] += 1
            if target_key in candidates_by_target:
                counts["duplicate_occurrences"] += 1
                if (
                    equivalent_candidate_payload(candidates_by_target[target_key])
                    != equivalent_candidate_payload(candidate)
                ):
                    counts["other_conflicts"] += 1
                    counts["divergent_duplicate_targets"] += 1
            else:
                candidates_by_target[target_key] = candidate

    candidates = list(candidates_by_target.values())
    conflicts = (
        counts["identity_conflicts"]
        + counts["unparseable_returned_at"]
        + counts["ambiguous_chronology"]
        + counts["other_conflicts"]
    )
    missing_scan_targets = sum(candidate["kind"] == "missing_scan" for candidate in candidates)
    missing_return_targets = sum(candidate["kind"] == "missing_return" for candidate in candidates)
    summary = {
        "schema_version": 1,
        "mode": "plan_counts_only",
        **counts,
        "missing_scan_targets": missing_scan_targets,
        "missing_return_targets": missing_return_targets,
        "scan_inserts": missing_scan_targets,
        "outbound_inserts": missing_scan_targets,
        "return_inserts": missing_scan_targets + missing_return_targets,
        "conflicts": conflicts,
    }
    summary["mutations_expected"] = summary["scan_inserts"] + summary["outbound_inserts"] + summary["return_inserts"]
    summary["safe_to_repair"] = conflicts == 0
    summary["plan_sha256"] = plan_hash(candidates)
    return summary, candidates


def deterministic_uuid(kind, *values):
    identity = "|".join([kind, *(str(value) for value in values)])
    return uuid.uuid5(NAMESPACE, identity)


def apply_candidates(db, candidates, summary):
    from sqlalchemy import select

    models = backend_module("models")
    AuditLog, KizCode, KizMovement, ScanCode = (
        models.AuditLog,
        models.KizCode,
        models.KizMovement,
        models.ScanCode,
    )
    scan_metadata_for_code = backend_module("scan_quantities").scan_metadata_for_code

    scan_inserts = 0
    outbound_inserts = 0
    return_inserts = 0
    for candidate in candidates:
        code = candidate["code"]
        item = candidate["item"]
        scan = candidate["scan"]
        if scan is None:
            scan_id = deterministic_uuid("scan", item.id, code)
            scan = db.get(ScanCode, scan_id)
            if scan is None:
                scan = ScanCode(
                    id=scan_id,
                    order_item_id=item.id,
                    code=code,
                    source="google_sheets_return_repair",
                    scanned_at=candidate["scan_at"],
                    raw_payload={
                        "cutover_repair": "historical_google_return_v1",
                        "timestamp_provenance": "synthetic_before_return",
                        **scan_metadata_for_code(code),
                    },
                )
                db.add(scan)
                item.scanned_blocks = candidate["new_scanned_blocks"]
                db.flush()
                scan_inserts += 1

        kiz = db.execute(select(KizCode).where(KizCode.code == code)).scalar_one_or_none()
        if kiz is None:
            kiz = KizCode(id=deterministic_uuid("kiz", code), code=code)
            db.add(kiz)
            db.flush()

        if candidate["kind"] == "missing_scan":
            outbound_id = deterministic_uuid("movement", scan.id, candidate["outbound_type"])
            if db.get(KizMovement, outbound_id) is None:
                db.add(KizMovement(
                    id=outbound_id,
                    kiz_id=kiz.id,
                    movement_type=candidate["outbound_type"],
                    order_id=item.order.id,
                    order_item_id=item.id,
                    scan_code_id=scan.id,
                    source="google_sheets_return_repair",
                    actor="phase27_deploy",
                    occurred_at=candidate["scan_at"],
                    raw_payload={
                        "cutover_repair": "historical_google_return_v1",
                        "timestamp_provenance": "synthetic_before_return",
                    },
                ))
                outbound_inserts += 1

        return_id = deterministic_uuid("movement", scan.id, "return")
        if db.get(KizMovement, return_id) is None:
            db.add(KizMovement(
                id=return_id,
                kiz_id=kiz.id,
                movement_type="return",
                order_id=item.order.id,
                order_item_id=item.id,
                scan_code_id=scan.id,
                return_reference=normalize(candidate["record"].get("return_reference")) or None,
                source="google_sheets_return_repair",
                actor=normalize(candidate["record"].get("returned_by")) or "phase27_deploy",
                occurred_at=candidate["return_at"],
                raw_payload={
                    "cutover_repair": "historical_google_return_v1",
                    "timestamp_provenance": candidate["timestamp_provenance"],
                    "timestamp_adjusted": bool(candidate["timestamp_adjusted"]),
                    "original_returned_at": candidate["original_return_at"].isoformat(),
                },
            ))
            return_inserts += 1

    expected_counts = {
        "scan_inserts": int(summary.get("scan_inserts") or 0),
        "outbound_inserts": int(summary.get("outbound_inserts") or 0),
        "return_inserts": int(summary.get("return_inserts") or 0),
    }
    actual_counts = {
        "scan_inserts": scan_inserts,
        "outbound_inserts": outbound_inserts,
        "return_inserts": return_inserts,
    }
    if actual_counts != expected_counts:
        raise RuntimeError("repair mutation count invariant failed")

    db.flush()
    for candidate in candidates:
        item = candidate["item"]
        scan_id = (
            candidate["scan"].id
            if candidate.get("scan") is not None
            else deterministic_uuid("scan", item.id, candidate["code"])
        )
        stored_scan = db.get(ScanCode, scan_id)
        if (
            stored_scan is None
            or str(stored_scan.order_item_id) != str(item.id)
            or normalize(stored_scan.code) != candidate["code"]
        ):
            raise RuntimeError("repair scan invariant failed")
        if candidate["kind"] == "missing_scan":
            outbound = db.get(
                KizMovement,
                deterministic_uuid("movement", scan_id, candidate["outbound_type"]),
            )
            if (
                outbound is None
                or normalize(outbound.movement_type) != candidate["outbound_type"]
                or str(outbound.order_id) != str(item.order.id)
                or str(outbound.order_item_id) != str(item.id)
                or str(outbound.scan_code_id) != str(scan_id)
            ):
                raise RuntimeError("repair outbound invariant failed")
        returned = db.get(KizMovement, deterministic_uuid("movement", scan_id, "return"))
        if (
            returned is None
            or normalize(returned.movement_type) != "return"
            or str(returned.order_id) != str(item.order.id)
            or str(returned.order_item_id) != str(item.id)
            or str(returned.scan_code_id) != str(scan_id)
        ):
            raise RuntimeError("repair return invariant failed")

    audit_id = deterministic_uuid("audit", summary["plan_sha256"])
    if db.get(AuditLog, audit_id) is None:
        db.add(AuditLog(
            id=audit_id,
            action="google_cutover_return_repair",
            entity_type="google_cutover",
            entity_id="historical_google_return_v1",
            payload={
                "plan_sha256": summary["plan_sha256"],
                "scan_inserts": scan_inserts,
                "outbound_inserts": outbound_inserts,
                "return_inserts": return_inserts,
                "values_redacted": True,
            },
        ))
    db.commit()
    return {
        "schema_version": 1,
        "mode": "apply_counts_only",
        "safe_to_repair": True,
        "plan_sha256": summary["plan_sha256"],
        "conflicts": 0,
        "scan_inserts": scan_inserts,
        "outbound_inserts": outbound_inserts,
        "return_inserts": return_inserts,
        "mutations_applied": scan_inserts + outbound_inserts + return_inserts,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--approval", default="")
    parser.add_argument("--expected-plan-sha", default="")
    parser.add_argument("--expected-missing-scans", type=int, required=True)
    parser.add_argument("--expected-missing-returns", type=int, required=True)
    args = parser.parse_args(argv)
    if args.expected_missing_scans < 0 or args.expected_missing_returns < 0:
        parser.error("expected counts must be non-negative")
    return args


def expected_counts_match(summary, args):
    return (
        summary["missing_scan_occurrences"] == args.expected_missing_scans
        and summary["missing_return_occurrences"] == args.expected_missing_returns
    )


def create_plan(db):
    records_and_items, identity_diagnostics = load_records_and_items(db)
    scans_by_code, movements_by_code = load_runtime_state(db, records_and_items)
    return build_repair_plan(
        records_and_items,
        scans_by_code,
        movements_by_code,
        identity_conflicts=sum(identity_diagnostics.values()),
        identity_diagnostics=identity_diagnostics,
    )


def run(argv=None):
    from sqlalchemy import select, text

    SessionLocal = backend_module("db").SessionLocal
    lock_kiz_codes_for_transaction = backend_module("kiz_movements_service").lock_kiz_codes_for_transaction
    OrderItem = backend_module("models").OrderItem

    args = parse_args(argv)
    with SessionLocal() as db:
        if args.apply:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), {
                "identity": "taksklad:google-cutover-return-repair:v1",
            })
        summary, candidates = create_plan(db)
        if not summary["safe_to_repair"] or not expected_counts_match(summary, args):
            db.rollback()
            print(json.dumps(summary, sort_keys=True))
            return 3
        if args.plan:
            db.rollback()
            print(json.dumps(summary, sort_keys=True))
            return 0
        if args.approval != APPROVAL or args.expected_plan_sha != summary["plan_sha256"]:
            db.rollback()
            print(json.dumps({**summary, "mode": "approval_rejected_counts_only"}, sort_keys=True))
            return 4

        lock_kiz_codes_for_transaction(db, [candidate["code"] for candidate in candidates])
        item_ids = sorted({candidate["item"].id for candidate in candidates}, key=str)
        if item_ids:
            db.execute(select(OrderItem.id).where(OrderItem.id.in_(item_ids)).with_for_update()).all()
        locked_summary, locked_candidates = create_plan(db)
        if (
            not locked_summary["safe_to_repair"]
            or locked_summary["plan_sha256"] != args.expected_plan_sha
            or not expected_counts_match(locked_summary, args)
        ):
            db.rollback()
            print(json.dumps({**locked_summary, "mode": "plan_changed_counts_only"}, sort_keys=True))
            return 5
        result = apply_candidates(db, locked_candidates, locked_summary)
        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "mode": "failed_counts_only",
            "safe_to_repair": False,
            "conflicts": -1,
            "error_type": type(exc).__name__,
        }, sort_keys=True))
        raise SystemExit(6)
