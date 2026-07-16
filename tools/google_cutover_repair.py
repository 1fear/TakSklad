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
BLOCKING_IDENTITY_DIAGNOSTIC_FIELDS = (
    "identity_no_strong_id_records",
    "identity_not_found_records",
    "identity_product_quantity_mismatch_records",
    "identity_multiple_records",
    "identity_order_not_returned_records",
)
IDENTITY_INFO_FIELDS = (
    "identity_multiple_with_return_codes_records",
    "identity_multiple_unique_scan_owner_records",
    "identity_multiple_unique_row_owner_records",
    "identity_multiple_codes_without_candidate_scan_occurrences",
    "identity_multiple_codes_with_multiple_candidate_scans_occurrences",
    "identity_multiple_codes_split_candidate_records",
    "identity_multiple_pool_size_two_records",
    "identity_multiple_pool_size_three_plus_records",
    "identity_multiple_unique_both_source_ids_records",
    "identity_multiple_unique_source_file_row_records",
    "identity_multiple_single_unique_signal_records",
    "identity_multiple_signal_agreement_records",
    "identity_multiple_signal_conflict_records",
    "identity_multiple_legacy_first_matches_signal_records",
    "identity_multiple_all_candidates_complete_records",
    "identity_multiple_exactly_one_candidate_missing_return_records",
    "identity_multiple_multiple_candidates_missing_return_records",
    "identity_multiple_invalid_lifecycle_records",
    "identity_multiple_source_ids_complete_row_complete_records",
    "identity_multiple_source_ids_missing_row_complete_records",
    "identity_multiple_source_ids_complete_row_missing_records",
    "identity_multiple_source_ids_missing_row_missing_records",
    "identity_multiple_source_row_lifecycle_invalid_records",
)
IDENTITY_DIAGNOSTIC_FIELDS = (
    *BLOCKING_IDENTITY_DIAGNOSTIC_FIELDS,
    *IDENTITY_INFO_FIELDS,
)
AMBIGUOUS_ERRORS = {
    "target_missing_movement_timestamp",
    "target_return_crosses_later_movement",
    "target_return_crosses_later_re_outbound_other_item",
    "target_return_crosses_later_outbound_other_item",
    "target_return_crosses_later_available_movement",
    "target_return_crosses_later_same_item_movement",
    "target_return_crosses_later_other_movement",
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
TARGET_DIAGNOSTIC_FIELDS = (
    "busy_previous_outbound_occurrences",
    "busy_previous_re_outbound_occurrences",
    "busy_previous_owner_order_returned_occurrences",
    "busy_previous_owner_matches_both_source_ids_occurrences",
    "busy_previous_owner_matches_google_row_occurrences",
    "busy_previous_owner_product_quantity_match_occurrences",
    "busy_previous_owner_scan_matches_movement_occurrences",
    "cross_later_backend_timestamp_in_interval_occurrences",
    "cross_later_google_timestamp_in_interval_occurrences",
    "cross_later_unique_audit_timestamp_in_interval_occurrences",
    "cross_later_no_trusted_timestamp_in_interval_occurrences",
    "cross_later_one_unique_trusted_timestamp_in_interval_occurrences",
    "cross_later_multiple_trusted_timestamps_in_interval_occurrences",
    "cross_later_all_trusted_timestamps_agree_occurrences",
)
CODE_LIFECYCLE_DIAGNOSTIC_FIELDS = (
    "code_owner_conflict_both_complete_occurrences",
    "code_owner_conflict_first_complete_current_missing_return_occurrences",
    "code_owner_conflict_first_missing_return_current_complete_occurrences",
    "code_owner_conflict_both_missing_return_occurrences",
    "code_owner_conflict_invalid_lifecycle_occurrences",
)


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


def parse_timestamp(raw_value):
    tashkent = ZoneInfo("Asia/Tashkent")
    text = normalize(raw_value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=tashkent).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def parse_returned_at(order, record):
    payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    for raw_value, provenance in (
        (payload.get("returned_at"), "backend_order"),
        (record.get("returned_at"), "google_sheet"),
    ):
        parsed = parse_timestamp(raw_value)
        if parsed is not None:
            return parsed, provenance
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
    identity_contexts = []
    identity_diagnostics = {field: 0 for field in IDENTITY_DIAGNOSTIC_FIELDS}
    for record in records:
        source_import_id = normalize(record.get("source_import_id"))
        source_order_id = normalize(record.get("source_order_id"))
        import_candidates = [
            item for item in by_import.get(source_import_id, [])
            if product_quantity_match(item, record)
        ] if source_import_id else []
        order_candidates = [
            item for item in by_order.get(source_order_id, [])
            if product_quantity_match(item, record)
        ] if source_order_id else []
        if source_import_id:
            pool = by_import.get(source_import_id, [])
            candidates = import_candidates
        elif source_order_id:
            pool = by_order.get(source_order_id, [])
            candidates = order_candidates
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
            if len(candidates) > 1:
                identity_contexts.append({
                    "record": record,
                    "candidates": candidates,
                    "import_candidates": import_candidates,
                    "order_candidates": order_candidates,
                })
                if len(candidates) == 2:
                    identity_diagnostics["identity_multiple_pool_size_two_records"] += 1
                else:
                    identity_diagnostics["identity_multiple_pool_size_three_plus_records"] += 1
                codes = list(dict.fromkeys(
                    normalize(value)
                    for value in (record.get("scanned_codes") or [])
                    if normalize(value)
                ))
                if codes:
                    identity_diagnostics["identity_multiple_with_return_codes_records"] += 1
                unique_scan_owners = []
                signal_owner_ids = []
                for code in codes:
                    owners = [
                        candidate
                        for candidate in candidates
                        if any(normalize(scan.code) == code for scan in (candidate.scan_codes or []))
                    ]
                    if not owners:
                        identity_diagnostics[
                            "identity_multiple_codes_without_candidate_scan_occurrences"
                        ] += 1
                    elif len(owners) > 1:
                        identity_diagnostics[
                            "identity_multiple_codes_with_multiple_candidate_scans_occurrences"
                        ] += 1
                    else:
                        unique_scan_owners.append(owners[0])
                if codes and len(unique_scan_owners) == len(codes):
                    owner_ids = {str(candidate.id) for candidate in unique_scan_owners}
                    if len(owner_ids) == 1:
                        identity_diagnostics["identity_multiple_unique_scan_owner_records"] += 1
                        signal_owner_ids.append(next(iter(owner_ids)))
                    else:
                        identity_diagnostics["identity_multiple_codes_split_candidate_records"] += 1

                record_row = int(record.get("row_number") or 0)
                record_sheet = normalize(record.get("source_sheet"))
                row_matches = []
                if record_row > 0 and record_sheet:
                    for candidate in candidates:
                        payload = candidate.raw_payload if isinstance(candidate.raw_payload, dict) else {}
                        if (
                            int(payload.get("google_sheet_row_number") or 0) == record_row
                            and normalize(payload.get("google_sheet_source_sheet")) == record_sheet
                        ):
                            row_matches.append(candidate)
                if len(row_matches) == 1:
                    identity_diagnostics["identity_multiple_unique_row_owner_records"] += 1
                    signal_owner_ids.append(str(row_matches[0].id))

                if source_import_id and source_order_id:
                    order_ids = {str(candidate.id) for candidate in order_candidates}
                    both_id_matches = [
                        candidate
                        for candidate in import_candidates
                        if str(candidate.id) in order_ids
                    ]
                    if len(both_id_matches) == 1:
                        identity_diagnostics["identity_multiple_unique_both_source_ids_records"] += 1
                        signal_owner_ids.append(str(both_id_matches[0].id))

                source_file = normalize(record.get("source_file"))
                source_row = normalize(record.get("source_row"))
                source_matches = []
                if source_file and source_row:
                    for candidate in candidates:
                        payload = candidate.raw_payload if isinstance(candidate.raw_payload, dict) else {}
                        if (
                            normalize(payload.get("source_file")) == source_file
                            and normalize(payload.get("source_row")) == source_row
                        ):
                            source_matches.append(candidate)
                if len(source_matches) == 1:
                    identity_diagnostics["identity_multiple_unique_source_file_row_records"] += 1
                    signal_owner_ids.append(str(source_matches[0].id))

                distinct_signal_owners = set(signal_owner_ids)
                if len(signal_owner_ids) == 1:
                    identity_diagnostics["identity_multiple_single_unique_signal_records"] += 1
                elif len(signal_owner_ids) >= 2 and len(distinct_signal_owners) == 1:
                    identity_diagnostics["identity_multiple_signal_agreement_records"] += 1
                    if pool and str(pool[0].id) in distinct_signal_owners:
                        identity_diagnostics[
                            "identity_multiple_legacy_first_matches_signal_records"
                        ] += 1
                elif len(distinct_signal_owners) > 1:
                    identity_diagnostics["identity_multiple_signal_conflict_records"] += 1
        matched.append((record, item))
    return matched, identity_diagnostics, identity_contexts, {
        str(item.id): item for item in items
    }


def load_records_and_items(db):
    records = backend_module("google_sheets_sync_worker").load_google_sheet_records()
    return match_records_to_items(db, records)


def load_runtime_state(db, records_and_items, identity_contexts=None, all_items=None):
    from sqlalchemy import select

    models = backend_module("models")
    AuditLog, KizCode, KizMovement, ScanCode = (
        models.AuditLog,
        models.KizCode,
        models.KizMovement,
        models.ScanCode,
    )

    codes = sorted({
        normalize(code)
        for record, _item in records_and_items
        if is_returned_record(record)
        for code in (record.get("scanned_codes") or [])
        if normalize(code)
    })
    scans_by_code = defaultdict(list)
    movements_by_code = defaultdict(list)
    audit_return_times = defaultdict(list)
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
    relevant_items = {
        str(item.id): item
        for _record, item in records_and_items
        if item is not None
    }
    for context in identity_contexts or []:
        for item in context["candidates"]:
            relevant_items[str(item.id)] = item
    order_ids = sorted({str(item.order.id) for item in relevant_items.values()})
    if order_ids:
        for audit in db.execute(
            select(AuditLog)
            .where(AuditLog.action == "order_returned")
            .where(AuditLog.entity_type == "order")
            .where(AuditLog.entity_id.in_(order_ids))
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars():
            payload = audit.payload if isinstance(audit.payload, dict) else {}
            parsed = parse_timestamp(payload.get("returned_at"))
            if parsed is not None:
                audit_return_times[normalize(audit.entity_id)].append(parsed)
    return (
        scans_by_code,
        movements_by_code,
        audit_return_times,
        dict(all_items or relevant_items),
    )


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


def item_code_lifecycle_state(item, code, movements_by_code):
    scans = [
        scan for scan in (item.scan_codes or [])
        if normalize(scan.code) == code
    ]
    if len(scans) != 1:
        return "invalid"
    scan = scans[0]
    movements = list(movements_by_code.get(code) or [])
    outbounds = [
        movement for movement in movements_for_scan(movements, scan, OUTBOUND_MOVEMENTS)
        if movement_matches_item(movement, item)
    ]
    returns = [
        movement for movement in movements_for_scan(movements, scan, {"return"})
        if movement_matches_item(movement, item)
    ]
    if len(outbounds) != 1:
        return "invalid"
    outbound_at = movement_time(outbounds[0])
    if outbound_at is None:
        return "invalid"
    if any(movement_time(movement) is None for movement in movements):
        return "invalid"
    if not returns:
        if any(
            movement.id != outbounds[0].id
            and movement_time(movement) >= outbound_at
            for movement in movements
        ):
            return "invalid"
        return "missing_return"
    if len(returns) != 1:
        return "invalid"
    return_at = movement_time(returns[0])
    if return_at is None or return_at <= outbound_at:
        return "invalid"
    if any(
        movement.id not in {outbounds[0].id, returns[0].id}
        and outbound_at <= movement_time(movement) <= return_at
        for movement in movements
    ):
        return "invalid"
    return "complete"


def aggregate_item_record_lifecycle(item, record, movements_by_code):
    codes = list(dict.fromkeys(
        normalize(value)
        for value in (record.get("scanned_codes") or [])
        if normalize(value)
    ))
    if not codes:
        return "invalid"
    states = [item_code_lifecycle_state(item, code, movements_by_code) for code in codes]
    if all(state == "complete" for state in states):
        return "complete"
    if all(state == "missing_return" for state in states):
        return "missing_return"
    return "invalid"


def unique_source_ids_candidate(context):
    record = context["record"]
    if not normalize(record.get("source_import_id")) or not normalize(record.get("source_order_id")):
        return None
    order_ids = {str(candidate.id) for candidate in context["order_candidates"]}
    matches = [
        candidate for candidate in context["import_candidates"]
        if str(candidate.id) in order_ids
    ]
    return matches[0] if len(matches) == 1 else None


def unique_google_row_candidate(context):
    record = context["record"]
    row_number = int(record.get("row_number") or 0)
    source_sheet = normalize(record.get("source_sheet"))
    if row_number <= 0 or not source_sheet:
        return None
    matches = []
    for candidate in context["candidates"]:
        payload = candidate.raw_payload if isinstance(candidate.raw_payload, dict) else {}
        if (
            int(payload.get("google_sheet_row_number") or 0) == row_number
            and normalize(payload.get("google_sheet_source_sheet")) == source_sheet
        ):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def enrich_identity_lifecycle_diagnostics(identity_diagnostics, contexts, movements_by_code):
    for context in contexts:
        record = context["record"]
        states = {
            str(candidate.id): aggregate_item_record_lifecycle(candidate, record, movements_by_code)
            for candidate in context["candidates"]
        }
        values = list(states.values())
        missing_count = sum(value == "missing_return" for value in values)
        if values and all(value == "complete" for value in values):
            identity_diagnostics["identity_multiple_all_candidates_complete_records"] += 1
        elif missing_count == 1 and all(value in {"complete", "missing_return"} for value in values):
            identity_diagnostics[
                "identity_multiple_exactly_one_candidate_missing_return_records"
            ] += 1
        elif missing_count > 1 and all(value in {"complete", "missing_return"} for value in values):
            identity_diagnostics["identity_multiple_multiple_candidates_missing_return_records"] += 1
        else:
            identity_diagnostics["identity_multiple_invalid_lifecycle_records"] += 1

        source_candidate = unique_source_ids_candidate(context)
        row_candidate = unique_google_row_candidate(context)
        if source_candidate is None or row_candidate is None:
            identity_diagnostics["identity_multiple_source_row_lifecycle_invalid_records"] += 1
            continue
        source_state = states.get(str(source_candidate.id), "invalid")
        row_state = states.get(str(row_candidate.id), "invalid")
        field = {
            ("complete", "complete"): "identity_multiple_source_ids_complete_row_complete_records",
            ("missing_return", "complete"): "identity_multiple_source_ids_missing_row_complete_records",
            ("complete", "missing_return"): "identity_multiple_source_ids_complete_row_missing_records",
            ("missing_return", "missing_return"): "identity_multiple_source_ids_missing_row_missing_records",
        }.get((source_state, row_state))
        if field:
            identity_diagnostics[field] += 1
        else:
            identity_diagnostics["identity_multiple_source_row_lifecycle_invalid_records"] += 1


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
            next_movement = later[0]
            next_type = normalize(next_movement.movement_type)
            if movement_matches_item(next_movement, item):
                error = "target_return_crosses_later_same_item_movement"
            elif next_type == "re_outbound":
                error = "target_return_crosses_later_re_outbound_other_item"
            elif next_type == "outbound":
                error = "target_return_crosses_later_outbound_other_item"
            elif next_type in AVAILABLE_MOVEMENTS:
                error = "target_return_crosses_later_available_movement"
            else:
                error = "target_return_crosses_later_other_movement"
            return None, error
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


def target_error_diagnostics(
    item,
    record,
    code,
    error,
    movements_by_code,
    *,
    audit_return_times=None,
    relevant_items=None,
):
    diagnostics = {field: 0 for field in TARGET_DIAGNOSTIC_FIELDS}
    movements = list(movements_by_code.get(code) or [])
    return_at, _provenance = parse_returned_at(item.order, record)
    if return_at is None:
        return diagnostics

    if error == "busy_before_missing_scan":
        before = [movement for movement in movements if movement_time(movement) < return_at]
        previous = before[-1] if before else None
        if previous is None:
            return diagnostics
        previous_type = normalize(previous.movement_type)
        if previous_type == "outbound":
            diagnostics["busy_previous_outbound_occurrences"] = 1
        elif previous_type == "re_outbound":
            diagnostics["busy_previous_re_outbound_occurrences"] = 1
        owner = (relevant_items or {}).get(str(previous.order_item_id or ""))
        if owner is None:
            return diagnostics
        diagnostics["busy_previous_owner_order_returned_occurrences"] = int(
            order_is_returned(owner.order)
        )
        owner_payload = owner.raw_payload if isinstance(owner.raw_payload, dict) else {}
        owner_import = normalize(owner.source_import_id) or normalize(owner_payload.get("source_import_id"))
        owner_order = normalize(owner_payload.get("source_order_id"))
        diagnostics["busy_previous_owner_matches_both_source_ids_occurrences"] = int(
            bool(normalize(record.get("source_import_id")))
            and bool(normalize(record.get("source_order_id")))
            and owner_import == normalize(record.get("source_import_id"))
            and owner_order == normalize(record.get("source_order_id"))
        )
        diagnostics["busy_previous_owner_product_quantity_match_occurrences"] = int(
            product_quantity_match(owner, record)
        )
        record_row = int(record.get("row_number") or 0)
        record_sheet = normalize(record.get("source_sheet"))
        diagnostics["busy_previous_owner_matches_google_row_occurrences"] = int(
            record_row > 0
            and bool(record_sheet)
            and int(owner_payload.get("google_sheet_row_number") or 0) == record_row
            and normalize(owner_payload.get("google_sheet_source_sheet")) == record_sheet
        )
        diagnostics["busy_previous_owner_scan_matches_movement_occurrences"] = int(any(
            normalize(scan.code) == code and str(scan.id) == str(previous.scan_code_id or "")
            for scan in (owner.scan_codes or [])
        ))
        return diagnostics

    if not error.startswith("target_return_crosses_later_"):
        return diagnostics
    item_scans = [scan for scan in (item.scan_codes or []) if normalize(scan.code) == code]
    if len(item_scans) != 1:
        return diagnostics
    outbound_movements = movements_for_scan(movements, item_scans[0], OUTBOUND_MOVEMENTS)
    if len(outbound_movements) != 1:
        return diagnostics
    outbound = outbound_movements[0]
    outbound_at = movement_time(outbound)
    later = [
        movement for movement in movements
        if movement.id != outbound.id and movement_time(movement) >= outbound_at
    ]
    if not later:
        return diagnostics
    next_at = movement_time(later[0])
    if outbound_at is None or next_at is None or next_at <= outbound_at:
        return diagnostics

    payload = item.order.raw_payload if isinstance(item.order.raw_payload, dict) else {}
    backend_at = parse_timestamp(payload.get("returned_at"))
    google_at = parse_timestamp(record.get("returned_at"))
    audit_values = list((audit_return_times or {}).get(str(item.order.id), []))
    backend_in = backend_at is not None and outbound_at < backend_at < next_at
    google_in = google_at is not None and outbound_at < google_at < next_at
    audit_in = sorted({value for value in audit_values if outbound_at < value < next_at})
    diagnostics["cross_later_backend_timestamp_in_interval_occurrences"] = int(backend_in)
    diagnostics["cross_later_google_timestamp_in_interval_occurrences"] = int(google_in)
    diagnostics["cross_later_unique_audit_timestamp_in_interval_occurrences"] = int(
        len(audit_in) == 1
    )
    trusted = [
        *([backend_at] if backend_in else []),
        *([google_at] if google_in else []),
        *audit_in,
    ]
    unique_trusted = set(trusted)
    diagnostics["cross_later_no_trusted_timestamp_in_interval_occurrences"] = int(
        not trusted
    )
    diagnostics["cross_later_one_unique_trusted_timestamp_in_interval_occurrences"] = int(
        len(unique_trusted) == 1
    )
    diagnostics["cross_later_multiple_trusted_timestamps_in_interval_occurrences"] = int(
        len(unique_trusted) > 1
    )
    diagnostics["cross_later_all_trusted_timestamps_agree_occurrences"] = int(
        len(trusted) >= 2 and len(unique_trusted) == 1
    )
    return diagnostics


def build_repair_plan(
    records_and_items,
    scans_by_code,
    movements_by_code,
    *,
    identity_conflicts=0,
    identity_diagnostics=None,
    audit_return_times=None,
    relevant_items=None,
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
        "code_owner_conflict_both_items_have_scan_occurrences": 0,
        "code_owner_conflict_only_first_item_has_scan_occurrences": 0,
        "code_owner_conflict_only_current_item_has_scan_occurrences": 0,
        "code_owner_conflict_neither_item_has_scan_occurrences": 0,
        "divergent_duplicate_targets": 0,
        **{field: int(identity_diagnostics.get(field) or 0) for field in IDENTITY_DIAGNOSTIC_FIELDS},
        **{f"{error}_occurrences": 0 for error in sorted(AMBIGUOUS_ERRORS | OTHER_ERRORS)},
        **{field: 0 for field in TARGET_DIAGNOSTIC_FIELDS},
        **{field: 0 for field in CODE_LIFECYCLE_DIAGNOSTIC_FIELDS},
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
            item_has_scan = any(
                normalize(scan.code) == code for scan in (item.scan_codes or [])
            )
            owner = code_owner.setdefault(code, (item, item_has_scan))
            if str(owner[0].id) != str(item.id):
                counts["other_conflicts"] += 1
                counts["code_owner_conflicts"] += 1
                if owner[1] and item_has_scan:
                    field = "code_owner_conflict_both_items_have_scan_occurrences"
                elif owner[1]:
                    field = "code_owner_conflict_only_first_item_has_scan_occurrences"
                elif item_has_scan:
                    field = "code_owner_conflict_only_current_item_has_scan_occurrences"
                else:
                    field = "code_owner_conflict_neither_item_has_scan_occurrences"
                counts[field] += 1
                first_state = item_code_lifecycle_state(owner[0], code, movements_by_code)
                current_state = item_code_lifecycle_state(item, code, movements_by_code)
                lifecycle_field = {
                    ("complete", "complete"): "code_owner_conflict_both_complete_occurrences",
                    ("complete", "missing_return"): (
                        "code_owner_conflict_first_complete_current_missing_return_occurrences"
                    ),
                    ("missing_return", "complete"): (
                        "code_owner_conflict_first_missing_return_current_complete_occurrences"
                    ),
                    ("missing_return", "missing_return"): (
                        "code_owner_conflict_both_missing_return_occurrences"
                    ),
                }.get((first_state, current_state), "code_owner_conflict_invalid_lifecycle_occurrences")
                counts[lifecycle_field] += 1
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
                diagnostics = target_error_diagnostics(
                    item,
                    record,
                    code,
                    error,
                    movements_by_code,
                    audit_return_times=audit_return_times,
                    relevant_items=relevant_items,
                )
                for field, value in diagnostics.items():
                    counts[field] += int(value)
                if error == "unparseable_returned_at":
                    counts["unparseable_returned_at"] += 1
                elif error in AMBIGUOUS_ERRORS:
                    counts["ambiguous_chronology"] += 1
                    counts[f"{error}_occurrences"] += 1
                    if (
                        error.startswith("target_return_crosses_later_")
                        and error != "target_return_crosses_later_movement"
                    ):
                        counts["target_return_crosses_later_movement_occurrences"] += 1
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
    records_and_items, identity_diagnostics, identity_contexts, all_items = load_records_and_items(db)
    scans_by_code, movements_by_code, audit_return_times, relevant_items = load_runtime_state(
        db,
        records_and_items,
        identity_contexts,
        all_items,
    )
    enrich_identity_lifecycle_diagnostics(
        identity_diagnostics,
        identity_contexts,
        movements_by_code,
    )
    return build_repair_plan(
        records_and_items,
        scans_by_code,
        movements_by_code,
        identity_conflicts=sum(
            identity_diagnostics[field]
            for field in BLOCKING_IDENTITY_DIAGNOSTIC_FIELDS
        ),
        identity_diagnostics=identity_diagnostics,
        audit_return_times=audit_return_times,
        relevant_items=relevant_items,
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
