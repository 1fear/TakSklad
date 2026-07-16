#!/usr/bin/env python3
"""Fail-closed one-shot transfer of active Google-only KIZ scans to PostgreSQL.

The script is streamed into the still-running legacy backend.  Planning is
read-only and output is counts-only.  Apply mode uses one transaction, a
canonical plan hash, deterministic UUIDs, ordered KIZ locks and row locks.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone


APPROVAL = "GOOGLE_CUTOVER_ACTIVE_KIZ_REPAIR_APPROVED"
EXPECTED_ACTIVE_MISSING = 6
NAMESPACE = uuid.UUID("0ffbcd76-32a0-5a84-92c1-5f8865274074")
AVAILABLE_MOVEMENTS = {"return", "undo", "reset"}
INACTIVE_STATUSES = {"returned", "removed_from_google_sheet", "archived_no_kiz", "cancelled"}
COMPLETED_STATUSES = {"completed", "done", "closed"}
BUSY_MOVEMENTS = {"outbound", "re_outbound"}
ALL_MOVEMENTS = BUSY_MOVEMENTS | AVAILABLE_MOVEMENTS


def backend_module(name):
    try:
        return importlib.import_module(f"app.{name}")
    except ModuleNotFoundError as exc:
        if exc.name not in {"app", f"app.{name}"}:
            raise
        return importlib.import_module(f"backend.app.{name}")


def normalize(value):
    """Treat KIZ as opaque text; trim edge ASCII whitespace only."""

    return str(value or "").strip(" \t\r\n")


def is_returned_record(record):
    value = normalize(record.get("return_status")).casefold()
    return value in {"return", "returned", "возврат"} or "возврат" in value


def is_active_record(record):
    return not bool(record.get("archived")) and not is_returned_record(record)


def aware_utc(value):
    if value is None or getattr(value, "tzinfo", None) is None:
        return None
    return value.astimezone(timezone.utc)


def stored_utc(value):
    """Normalize DB-returned timestamps; SQLite test storage drops tzinfo."""

    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_observed_at(value):
    text = normalize(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit("ACTIVE_KIZ_OBSERVED_AT_INVALID") from exc
    parsed = aware_utc(parsed)
    if parsed is None:
        raise SystemExit("ACTIVE_KIZ_OBSERVED_AT_INVALID")
    now = datetime.now(timezone.utc)
    if parsed > now + timedelta(minutes=5) or parsed < now - timedelta(hours=2):
        raise SystemExit("ACTIVE_KIZ_OBSERVED_AT_OUT_OF_RANGE")
    return parsed


def google_snapshot_hash(records):
    """Hash only fields that can affect cutover selection; never emit values."""

    payload = []
    for record in records:
        payload.append({
            "archived": bool(record.get("archived")),
            "return_status": normalize(record.get("return_status")),
            "source_import_id": normalize(record.get("source_import_id")),
            "source_order_id": normalize(record.get("source_order_id")),
            "source_sheet": normalize(record.get("source_sheet")),
            "row_number": int(record.get("row_number") or 0),
            "product": normalize(record.get("product")),
            "quantity_blocks": int(record.get("quantity_blocks") or 0),
            "quantity_pieces": int(record.get("quantity_pieces") or 0),
            "scanned_codes": sorted({
                normalize(code)
                for code in (record.get("scanned_codes") or [])
                if normalize(code)
            }),
        })
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def lifecycle_conflict(movements, scans):
    """Return a counts-only reason for a malformed or busy lifecycle."""

    if not movements:
        return "orphan_registry" if scans == "registry_only" else ""
    previous = None
    previous_time = None
    for movement in movements:
        movement_type = normalize(getattr(movement, "movement_type", ""))
        occurred_at = movement_time(movement)
        if movement_type not in ALL_MOVEMENTS or occurred_at is None:
            return "malformed"
        if previous_time is not None and occurred_at <= previous_time:
            return "malformed"
        if movement_type in BUSY_MOVEMENTS:
            if previous is None and movement_type != "outbound":
                return "malformed"
            if previous is not None:
                previous_type = normalize(previous.movement_type)
                expected = "re_outbound" if previous_type == "return" else "outbound"
                if previous_type not in AVAILABLE_MOVEMENTS or movement_type != expected:
                    return "malformed"
            if not movement.order_id or not movement.order_item_id or not movement.scan_code_id:
                return "malformed"
        else:
            if previous is None or normalize(previous.movement_type) not in BUSY_MOVEMENTS:
                return "malformed"
            if movement.order_id and str(movement.order_id) != str(previous.order_id):
                return "malformed"
            if movement.order_item_id and str(movement.order_item_id) != str(previous.order_item_id):
                return "malformed"
            if movement_type == "return" and str(movement.scan_code_id or "") != str(previous.scan_code_id or ""):
                return "malformed"
        previous = movement
        previous_time = occurred_at
    return "busy" if normalize(movements[-1].movement_type) in BUSY_MOVEMENTS else ""


def product_quantity_match(item, record):
    product = normalize(record.get("product")).casefold()
    if not product or normalize(item.product).casefold() != product:
        return False
    blocks = int(record.get("quantity_blocks") or 0)
    pieces = int(record.get("quantity_pieces") or 0)
    if blocks <= 0 or int(item.quantity_blocks or 0) != blocks:
        return False
    if int(item.quantity_pieces or 0) != pieces:
        return False
    return True


def strong_candidates(record, by_import, by_order):
    source_import_id = normalize(record.get("source_import_id"))
    source_order_id = normalize(record.get("source_order_id"))
    import_candidates = list(by_import.get(source_import_id, [])) if source_import_id else []
    order_candidates = list(by_order.get(source_order_id, [])) if source_order_id else []
    if source_import_id and source_order_id:
        order_ids = {str(item.id) for item in order_candidates}
        candidates = [item for item in import_candidates if str(item.id) in order_ids]
    elif source_import_id:
        candidates = import_candidates
    elif source_order_id:
        candidates = order_candidates
    else:
        candidates = []
    return [item for item in candidates if product_quantity_match(item, record)]


def strong_identity_matches(item, record):
    imports_service = backend_module("imports_service")
    payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    source_import_id = normalize(record.get("source_import_id"))
    source_order_id = normalize(record.get("source_order_id"))
    if source_import_id:
        if normalize(item.source_import_id) != source_import_id:
            return False
        if normalize(payload.get("source_import_id")) != source_import_id:
            return False
        if normalize(item.source_import_key) != normalize(
            imports_service.source_import_lookup_key(source_import_id)
        ):
            return False
    if source_order_id and normalize(payload.get("source_order_id")) != source_order_id:
        return False
    return bool(source_import_id or source_order_id)


def load_active_targets(db):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload, selectinload

    OrderItem = backend_module("models").OrderItem
    worker = backend_module("google_sheets_sync_worker")
    records = worker.load_google_sheet_records()
    legacy_index = worker.load_item_index(db, records)
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

    diagnostics = {
        "active_records_total": 0,
        "active_missing_item_records": 0,
        "identity_no_strong_id_records": 0,
        "identity_not_unique_records": 0,
        "identity_mapping_mismatch_records": 0,
        "identity_product_quantity_mismatch_records": 0,
        "identity_hash_mismatch_records": 0,
    }
    targets = []
    for record in records:
        if not is_active_record(record):
            continue
        diagnostics["active_records_total"] += 1
        codes = [
            normalize(value)
            for value in (record.get("scanned_codes") or [])
            if normalize(value)
        ]
        if not codes:
            continue
        legacy_item = worker.find_item_for_record(record, legacy_index)
        if legacy_item is None:
            diagnostics["active_missing_item_records"] += 1
            continue
        existing = {
            normalize(scan.code)
            for scan in (legacy_item.scan_codes or [])
            if normalize(scan.code)
        }
        missing = [code for code in codes if code not in existing]
        if not missing:
            continue

        source_import_id = normalize(record.get("source_import_id"))
        source_order_id = normalize(record.get("source_order_id"))
        if not source_import_id and not source_order_id:
            diagnostics["identity_no_strong_id_records"] += 1
            continue
        candidates = strong_candidates(record, by_import, by_order)
        if len(candidates) != 1:
            raw_pool = (
                by_import.get(source_import_id, [])
                if source_import_id
                else by_order.get(source_order_id, [])
            )
            if raw_pool and not candidates:
                diagnostics["identity_product_quantity_mismatch_records"] += 1
            else:
                diagnostics["identity_not_unique_records"] += 1
            continue
        item = candidates[0]
        if not strong_identity_matches(item, record):
            diagnostics["identity_hash_mismatch_records"] += 1
            continue
        if str(item.id) != str(legacy_item.id):
            diagnostics["identity_mapping_mismatch_records"] += 1
            continue
        targets.append((record, item, missing))
    return targets, diagnostics, google_snapshot_hash(records)


def load_runtime_state(db, targets):
    from sqlalchemy import select

    models = backend_module("models")
    KizCode, KizMovement, ScanCode = models.KizCode, models.KizMovement, models.ScanCode
    codes = sorted({code for _record, _item, values in targets for code in values})
    scans = defaultdict(list)
    movements = defaultdict(list)
    registered_codes = set()
    if codes:
        registered_codes = {
            normalize(value)
            for value in db.execute(select(KizCode.code).where(KizCode.code.in_(codes))).scalars()
        }
        for scan in db.execute(
            select(ScanCode)
            .where(ScanCode.code.in_(codes))
            .order_by(ScanCode.scanned_at, ScanCode.id)
        ).scalars():
            scans[normalize(scan.code)].append(scan)
        for code, movement in db.execute(
            select(KizCode.code, KizMovement)
            .join(KizMovement, KizMovement.kiz_id == KizCode.id)
            .where(KizCode.code.in_(codes))
            .order_by(KizCode.code, KizMovement.occurred_at, KizMovement.id)
        ).all():
            movements[normalize(code)].append(movement)
    return scans, movements, registered_codes


def movement_time(movement):
    return aware_utc(getattr(movement, "occurred_at", None))


def deterministic_uuid(kind, *values):
    return uuid.uuid5(NAMESPACE, "|".join([kind, *(str(value) for value in values)]))


def candidate_payload(candidate):
    return {
        "code": candidate["code"],
        "order_id": str(candidate["item"].order.id),
        "item_id": str(candidate["item"].id),
        "source_import_id": normalize(candidate["record"].get("source_import_id")),
        "source_order_id": normalize(candidate["record"].get("source_order_id")),
        "source_sheet": normalize(candidate["record"].get("source_sheet")),
        "row_number": int(candidate["record"].get("row_number") or 0),
        "scan_at": candidate["scan_at"].isoformat(),
        "outbound_type": candidate["outbound_type"],
        "block_quantity": candidate["block_quantity"],
        "old_scanned_blocks": candidate["old_scanned_blocks"],
        "new_scanned_blocks": candidate["new_scanned_blocks"],
        "old_item_status": candidate["old_item_status"],
        "new_item_status": candidate["new_item_status"],
        "movement_history": [
            {
                "id": str(movement.id),
                "type": normalize(movement.movement_type),
                "occurred_at": movement_time(movement).isoformat(),
                "order_id": str(movement.order_id or ""),
                "item_id": str(movement.order_item_id or ""),
                "scan_id": str(movement.scan_code_id or ""),
            }
            for movement in candidate["movements"]
        ],
    }


def plan_hash(candidates, snapshot_sha):
    payload = {
        "google_snapshot_sha256": snapshot_sha,
        "candidates": [
            candidate_payload(value)
            for value in sorted(candidates, key=lambda value: (value["code"], str(value["item"].id)))
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_plan(
    targets,
    scans_by_code,
    movements_by_code,
    registered_codes=None,
    diagnostics=None,
    *,
    observed_at=None,
    snapshot_sha="",
):
    scan_quantities = backend_module("scan_quantities")
    registered_codes = set(registered_codes or ())
    observed_at = aware_utc(observed_at) or datetime.now(timezone.utc)
    counts = {
        "active_missing_code_occurrences": 0,
        "active_missing_unique_codes": 0,
        "active_missing_unique_item_codes": 0,
        "duplicate_target_occurrences": 0,
        "identity_conflicts": 0,
        "existing_same_item_scan_occurrences": 0,
        "existing_other_item_scan_without_available_movement_occurrences": 0,
        "busy_kiz_occurrences": 0,
        "movement_timestamp_conflicts": 0,
        "movement_scan_owner_conflicts": 0,
        "product_mismatch_occurrences": 0,
        "inactive_order_occurrences": 0,
        "inactive_item_occurrences": 0,
        "non_kiz_item_occurrences": 0,
        "missing_item_timestamp_occurrences": 0,
        "scanned_blocks_exceed_plan_occurrences": 0,
        "completed_item_under_plan_occurrences": 0,
        "invalid_block_quantity_occurrences": 0,
        "unknown_product_occurrences": 0,
        "orphan_registry_occurrences": 0,
        "malformed_lifecycle_occurrences": 0,
        "live_scan_after_undo_reset_occurrences": 0,
        "multiple_live_scans_occurrences": 0,
        "counter_inconsistency_occurrences": 0,
        "non_active_order_occurrences": 0,
        "non_active_item_occurrences": 0,
        "outbound_inserts": 0,
        "re_outbound_inserts": 0,
        "scan_inserts": 0,
        "kiz_code_inserts": 0,
        "item_scanned_blocks_updates": 0,
        "item_status_updates": 0,
    }
    diagnostics = dict(diagnostics or {})
    counts["identity_conflicts"] = sum(
        int(value)
        for key, value in diagnostics.items()
        if key != "active_records_total"
    )
    raw_targets = []
    target_keys = set()
    all_codes = []
    for record, item, missing_codes in targets:
        for code in missing_codes:
            counts["active_missing_code_occurrences"] += 1
            all_codes.append(code)
            key = (code, str(item.id))
            if key in target_keys:
                counts["duplicate_target_occurrences"] += 1
                continue
            target_keys.add(key)
            same_item = [scan for scan in scans_by_code.get(code, []) if str(scan.order_item_id) == str(item.id)]
            other_item = [scan for scan in scans_by_code.get(code, []) if str(scan.order_item_id) != str(item.id)]
            if len(scans_by_code.get(code, [])) > 1:
                counts["multiple_live_scans_occurrences"] += 1
                continue
            if same_item:
                counts["existing_same_item_scan_occurrences"] += 1
                continue
            movements = list(movements_by_code.get(code, []))
            sequence_conflict = lifecycle_conflict(
                movements,
                "registry_only" if code in registered_codes and not movements else "",
            )
            if sequence_conflict == "orphan_registry":
                counts["orphan_registry_occurrences"] += 1
                continue
            if sequence_conflict == "malformed":
                counts["malformed_lifecycle_occurrences"] += 1
                continue
            if sequence_conflict == "busy":
                counts["busy_kiz_occurrences"] += 1
                continue
            latest = movements[-1] if movements else None
            latest_type = normalize(latest.movement_type) if latest is not None else ""
            if latest is not None and movement_time(latest) >= observed_at:
                counts["movement_timestamp_conflicts"] += 1
                continue
            if other_item and latest is None:
                counts["existing_other_item_scan_without_available_movement_occurrences"] += 1
                continue
            if other_item and latest_type in {"undo", "reset"}:
                counts["live_scan_after_undo_reset_occurrences"] += 1
                continue
            if other_item and latest_type == "return":
                other_ids = {str(scan.id) for scan in other_item}
                if str(latest.scan_code_id or "") not in other_ids:
                    counts["movement_scan_owner_conflicts"] += 1
                    continue
            order_status = normalize(item.order.status)
            item_status = normalize(item.status)
            if order_status in INACTIVE_STATUSES:
                counts["inactive_order_occurrences"] += 1
                continue
            if item_status in INACTIVE_STATUSES:
                counts["inactive_item_occurrences"] += 1
                continue
            if not bool(item.requires_kiz):
                counts["non_kiz_item_occurrences"] += 1
                continue
            metadata = scan_quantities.scan_metadata_for_code(code)
            item_product_key = scan_quantities.product_key_from_name(item.product)
            code_product_key = normalize(metadata.get("product_key"))
            if not item_product_key or not code_product_key:
                counts["unknown_product_occurrences"] += 1
                continue
            if item_product_key != code_product_key:
                counts["product_mismatch_occurrences"] += 1
                continue
            if metadata.get("scan_type") == getattr(scan_quantities, "SCAN_TYPE_AGGREGATE_BOX", "aggregate_box"):
                product_key = scan_quantities.product_key_from_name(item.product)
                if not product_key or product_key != metadata.get("aggregate_product_key"):
                    counts["product_mismatch_occurrences"] += 1
                    continue
            if aware_utc(getattr(item, "created_at", None)) is None:
                counts["missing_item_timestamp_occurrences"] += 1
                continue
            block_quantity = int(metadata.get("block_quantity") or 0)
            if block_quantity <= 0:
                counts["invalid_block_quantity_occurrences"] += 1
                continue
            raw_targets.append({
                "record": record,
                "item": item,
                "code": code,
                "latest_movement": latest,
                "outbound_type": "re_outbound" if latest_type == "return" else "outbound",
                "scan_at": observed_at,
                "block_quantity": block_quantity,
                "scan_metadata": metadata,
                "movements": movements,
            })

    counts["active_missing_unique_codes"] = len(set(all_codes))
    counts["active_missing_unique_item_codes"] = len(target_keys)
    candidates = []
    by_item = defaultdict(list)
    for target in raw_targets:
        by_item[str(target["item"].id)].append(target)
    for item_targets in by_item.values():
        item = item_targets[0]["item"]
        existing_blocks = int(scan_quantities.scanned_blocks_for_scans(item.scan_codes or []))
        old_blocks = int(item.scanned_blocks or 0)
        calculated_blocks = existing_blocks + sum(value["block_quantity"] for value in item_targets)
        if old_blocks != existing_blocks:
            counts["counter_inconsistency_occurrences"] += len(item_targets)
            continue
        if normalize(item.order.status) != "not_completed":
            counts["non_active_order_occurrences"] += len(item_targets)
            continue
        if normalize(item.status) != "not_completed":
            counts["non_active_item_occurrences"] += len(item_targets)
            continue
        new_blocks = calculated_blocks
        quantity_blocks = int(item.quantity_blocks or 0)
        if quantity_blocks <= 0 or new_blocks > quantity_blocks:
            counts["scanned_blocks_exceed_plan_occurrences"] += len(item_targets)
            continue
        old_status = normalize(item.status)
        new_status = "completed" if new_blocks >= quantity_blocks else "not_completed"
        for target in item_targets:
            target.update({
                "old_scanned_blocks": old_blocks,
                "new_scanned_blocks": new_blocks,
                "old_item_status": old_status,
                "new_item_status": new_status,
            })
            candidates.append(target)
        counts["item_scanned_blocks_updates"] += int(old_blocks != new_blocks)
        counts["item_status_updates"] += int(old_status != new_status)

    counts["scan_inserts"] = len(candidates)
    counts["outbound_inserts"] = sum(value["outbound_type"] == "outbound" for value in candidates)
    counts["re_outbound_inserts"] = sum(value["outbound_type"] == "re_outbound" for value in candidates)
    counts["kiz_code_inserts"] = sum(value["code"] not in registered_codes for value in candidates)
    scope_conflicts = int(
        counts["active_missing_code_occurrences"] != EXPECTED_ACTIVE_MISSING
        or counts["active_missing_unique_codes"] != EXPECTED_ACTIVE_MISSING
        or counts["active_missing_unique_item_codes"] != EXPECTED_ACTIVE_MISSING
        or len(candidates) != EXPECTED_ACTIVE_MISSING
    )
    conflict_fields = (
        "identity_conflicts",
        "duplicate_target_occurrences",
        "existing_same_item_scan_occurrences",
        "existing_other_item_scan_without_available_movement_occurrences",
        "busy_kiz_occurrences",
        "movement_timestamp_conflicts",
        "movement_scan_owner_conflicts",
        "product_mismatch_occurrences",
        "inactive_order_occurrences",
        "inactive_item_occurrences",
        "non_kiz_item_occurrences",
        "missing_item_timestamp_occurrences",
        "scanned_blocks_exceed_plan_occurrences",
        "completed_item_under_plan_occurrences",
        "invalid_block_quantity_occurrences",
        "unknown_product_occurrences",
        "orphan_registry_occurrences",
        "malformed_lifecycle_occurrences",
        "live_scan_after_undo_reset_occurrences",
        "multiple_live_scans_occurrences",
        "counter_inconsistency_occurrences",
        "non_active_order_occurrences",
        "non_active_item_occurrences",
    )
    conflicts = scope_conflicts + sum(counts[field] for field in conflict_fields)
    summary = {
        "schema_version": 1,
        "mode": "plan_counts_only",
        **diagnostics,
        **counts,
        "scope_conflicts": scope_conflicts,
        "conflicts": conflicts,
        "mutations_expected": counts["scan_inserts"] + counts["outbound_inserts"] + counts["re_outbound_inserts"],
        "safe_to_repair": conflicts == 0,
        "google_snapshot_sha256": snapshot_sha,
        "plan_sha256": plan_hash(candidates, snapshot_sha),
    }
    return summary, candidates


def create_plan(db, observed_at):
    targets, diagnostics, snapshot_sha = load_active_targets(db)
    scans, movements, registered_codes = load_runtime_state(db, targets)
    return build_plan(
        targets,
        scans,
        movements,
        registered_codes,
        diagnostics,
        observed_at=observed_at,
        snapshot_sha=snapshot_sha,
    )


def apply_candidates(db, candidates, summary):
    from sqlalchemy import desc, select

    models = backend_module("models")
    AuditLog, KizCode, KizMovement, ScanCode = models.AuditLog, models.KizCode, models.KizMovement, models.ScanCode
    scan_inserts = 0
    movement_inserts = 0
    kiz_inserts = 0
    target_audit_inserts = 0
    item_updates = {}
    for candidate in candidates:
        code = candidate["code"]
        item = candidate["item"]
        kiz = db.execute(select(KizCode).where(KizCode.code == code)).scalar_one_or_none()
        if kiz is None:
            kiz_id = deterministic_uuid("kiz", code)
            existing_deterministic_kiz = db.get(KizCode, kiz_id)
            if existing_deterministic_kiz is not None:
                raise RuntimeError("active KIZ deterministic registry collision")
            kiz = KizCode(
                id=kiz_id,
                code=code,
                first_seen_at=candidate["scan_at"],
                updated_at=candidate["scan_at"],
            )
            db.add(kiz)
            db.flush()
            kiz_inserts += 1
        scan_id = deterministic_uuid("scan", item.id, code)
        if db.get(ScanCode, scan_id) is not None:
            raise RuntimeError("active KIZ deterministic scan already exists")
        scan = ScanCode(
            id=scan_id,
            order_item_id=item.id,
            code=code,
            source="google_sheets_cutover",
            scanned_by="google_cutover_active_kiz_repair",
            scanned_at=candidate["scan_at"],
            raw_payload={
                "cutover_repair": "active_google_kiz_v1",
                "google_sheet_row_number": int(candidate["record"].get("row_number") or 0),
                "google_sheet_source_sheet": normalize(candidate["record"].get("source_sheet")),
                "timestamp_provenance": "cutover_observed_at",
                **candidate["scan_metadata"],
            },
        )
        db.add(scan)
        db.flush()
        scan_inserts += 1
        movement_id = deterministic_uuid("movement", scan_id, candidate["outbound_type"])
        if db.get(KizMovement, movement_id) is not None:
            raise RuntimeError("active KIZ deterministic movement already exists")
        db.add(KizMovement(
            id=movement_id,
            kiz_id=kiz.id,
            movement_type=candidate["outbound_type"],
            order_id=item.order.id,
            order_item_id=item.id,
            scan_code_id=scan_id,
            source="google_sheets_cutover",
            actor="google_cutover_active_kiz_repair",
            occurred_at=candidate["scan_at"],
            raw_payload={
                "cutover_repair": "active_google_kiz_v1",
                "previous_movement_type": normalize(
                    getattr(candidate["movements"][-1] if candidate["movements"] else None, "movement_type", "")
                ),
                "timestamp_provenance": "cutover_observed_at",
                "scan_type": candidate["scan_metadata"].get("scan_type") or "",
                "block_quantity": candidate["block_quantity"],
            },
        ))
        movement_inserts += 1
        target_audit_id = deterministic_uuid("target_audit", scan_id, summary["plan_sha256"])
        if db.get(AuditLog, target_audit_id) is not None:
            raise RuntimeError("active KIZ deterministic target audit already exists")
        db.add(AuditLog(
            id=target_audit_id,
            action="google_cutover_active_kiz_scan_repaired",
            entity_type="scan_code",
            entity_id=str(scan_id),
            payload={
                "plan_sha256": summary["plan_sha256"],
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "order_id": str(item.order.id),
                "order_item_id": str(item.id),
                "movement_id": str(movement_id),
                "movement_type": candidate["outbound_type"],
                "scanned_at": candidate["scan_at"].isoformat(),
                "source": "google_sheets_cutover",
            },
        ))
        target_audit_inserts += 1
        item_updates[str(item.id)] = candidate

    for candidate in item_updates.values():
        item = candidate["item"]
        item.scanned_blocks = candidate["new_scanned_blocks"]
        item.status = candidate["new_item_status"]
    if scan_inserts != int(summary["scan_inserts"]):
        raise RuntimeError("active KIZ scan count invariant failed")
    if movement_inserts != int(summary["outbound_inserts"] + summary["re_outbound_inserts"]):
        raise RuntimeError("active KIZ movement count invariant failed")
    if kiz_inserts != int(summary["kiz_code_inserts"]):
        raise RuntimeError("active KIZ registry count invariant failed")
    if target_audit_inserts != int(summary["scan_inserts"]):
        raise RuntimeError("active KIZ target audit count invariant failed")

    db.flush()
    for candidate in candidates:
        item = candidate["item"]
        scan_id = deterministic_uuid("scan", item.id, candidate["code"])
        scan = db.get(ScanCode, scan_id)
        movement = db.get(
            KizMovement,
            deterministic_uuid("movement", scan_id, candidate["outbound_type"]),
        )
        latest = db.execute(
            select(KizMovement)
            .where(KizMovement.kiz_id == movement.kiz_id)
            .order_by(desc(KizMovement.occurred_at), desc(KizMovement.id))
            .limit(1)
        ).scalar_one_or_none() if movement is not None else None
        if (
            scan is None
            or str(scan.order_item_id) != str(item.id)
            or normalize(scan.code) != candidate["code"]
            or movement is None
            or normalize(movement.movement_type) != candidate["outbound_type"]
            or str(movement.order_id) != str(item.order.id)
            or str(movement.order_item_id) != str(item.id)
            or str(movement.scan_code_id) != str(scan_id)
            or stored_utc(scan.scanned_at) != candidate["scan_at"]
            or stored_utc(movement.occurred_at) != candidate["scan_at"]
            or latest is None
            or str(latest.id) != str(movement.id)
            or int(item.scanned_blocks or 0) != candidate["new_scanned_blocks"]
            or normalize(item.status) != candidate["new_item_status"]
        ):
            raise RuntimeError("active KIZ post-flush invariant failed")

    audit_id = deterministic_uuid("audit", summary["plan_sha256"])
    if db.get(AuditLog, audit_id) is not None:
        raise RuntimeError("active KIZ audit already exists")
    db.add(AuditLog(
        id=audit_id,
        action="google_cutover_active_kiz_repair",
        entity_type="google_cutover",
        entity_id="active_google_kiz_v1",
        payload={
            "plan_sha256": summary["plan_sha256"],
            "scan_inserts": scan_inserts,
            "outbound_inserts": int(summary["outbound_inserts"]),
            "re_outbound_inserts": int(summary["re_outbound_inserts"]),
            "kiz_code_inserts": kiz_inserts,
            "item_scanned_blocks_updates": int(summary["item_scanned_blocks_updates"]),
            "item_status_updates": int(summary["item_status_updates"]),
            "target_audit_inserts": target_audit_inserts,
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
        "outbound_inserts": int(summary["outbound_inserts"]),
        "re_outbound_inserts": int(summary["re_outbound_inserts"]),
        "kiz_code_inserts": kiz_inserts,
        "item_scanned_blocks_updates": int(summary["item_scanned_blocks_updates"]),
        "item_status_updates": int(summary["item_status_updates"]),
        "target_audit_inserts": target_audit_inserts,
        "mutations_applied": scan_inserts + movement_inserts,
    }


def verify_applied(db, expected_plan_sha, observed_at):
    from sqlalchemy import desc, select
    from sqlalchemy.orm import joinedload, selectinload

    models = backend_module("models")
    AuditLog, KizCode, KizMovement, OrderItem, ScanCode = (
        models.AuditLog,
        models.KizCode,
        models.KizMovement,
        models.OrderItem,
        models.ScanCode,
    )
    scan_quantities = backend_module("scan_quantities")
    db.expire_all()
    target_audits = [
        audit
        for audit in db.execute(
            select(AuditLog).where(AuditLog.action == "google_cutover_active_kiz_scan_repaired")
        ).scalars()
        if isinstance(audit.payload, dict) and audit.payload.get("plan_sha256") == expected_plan_sha
    ]
    batch_audits = [
        audit
        for audit in db.execute(
            select(AuditLog).where(AuditLog.action == "google_cutover_active_kiz_repair")
        ).scalars()
        if isinstance(audit.payload, dict) and audit.payload.get("plan_sha256") == expected_plan_sha
    ]
    conflicts = 0
    verified_scans = 0
    verified_movements = 0
    verified_latest_movements = 0
    verified_code_hashes = 0
    scan_codes = set()
    item_ids = set()
    if len(target_audits) != EXPECTED_ACTIVE_MISSING or len(batch_audits) != 1:
        conflicts += 1
    for audit in target_audits:
        payload = audit.payload
        try:
            scan_id = uuid.UUID(str(audit.entity_id))
            movement_id = uuid.UUID(str(payload.get("movement_id") or ""))
        except (TypeError, ValueError, AttributeError):
            conflicts += 1
            continue
        scan = db.get(ScanCode, scan_id)
        movement = db.get(KizMovement, movement_id)
        if scan is None or movement is None:
            conflicts += 1
            continue
        if (
            scan.source != "google_sheets_cutover"
            or normalize(scan.scanned_by) != "google_cutover_active_kiz_repair"
            or stored_utc(scan.scanned_at) != observed_at
        ):
            conflicts += 1
            continue
        verified_scans += 1
        if hashlib.sha256(normalize(scan.code).encode("utf-8")).hexdigest() != payload.get("code_sha256"):
            conflicts += 1
        else:
            verified_code_hashes += 1
        if (
            str(movement.scan_code_id or "") != str(scan.id)
            or str(movement.order_item_id or "") != str(scan.order_item_id)
            or normalize(movement.movement_type) != normalize(payload.get("movement_type"))
            or normalize(movement.movement_type) not in BUSY_MOVEMENTS
            or movement.source != "google_sheets_cutover"
            or normalize(movement.actor) != "google_cutover_active_kiz_repair"
            or stored_utc(movement.occurred_at) != observed_at
        ):
            conflicts += 1
            continue
        verified_movements += 1
        latest = db.execute(
            select(KizMovement)
            .join(KizCode, KizMovement.kiz_id == KizCode.id)
            .where(KizCode.code == scan.code)
            .order_by(desc(KizMovement.occurred_at), desc(KizMovement.id))
            .limit(1)
        ).scalar_one_or_none()
        if latest is None or str(latest.id) != str(movement.id):
            conflicts += 1
        else:
            verified_latest_movements += 1
        scan_codes.add(normalize(scan.code))
        item_ids.add(scan.order_item_id)

    verified_item_counters = 0
    if item_ids:
        items = db.execute(
            select(OrderItem)
            .options(joinedload(OrderItem.order), selectinload(OrderItem.scan_codes))
            .where(OrderItem.id.in_(sorted(item_ids, key=str)))
        ).scalars().all()
        if len(items) != len(item_ids):
            conflicts += 1
        for item in items:
            calculated = int(scan_quantities.scanned_blocks_for_scans(item.scan_codes or []))
            expected_status = "completed" if int(item.quantity_blocks or 0) > 0 and calculated >= int(item.quantity_blocks or 0) else "not_completed"
            if int(item.scanned_blocks or 0) != calculated or normalize(item.status) != expected_status:
                conflicts += 1
            else:
                verified_item_counters += 1
    if len(scan_codes) != EXPECTED_ACTIVE_MISSING:
        conflicts += 1
    if len(batch_audits) == 1:
        batch = batch_audits[0].payload
        if (
            int(batch.get("scan_inserts") or 0) != EXPECTED_ACTIVE_MISSING
            or int(batch.get("outbound_inserts") or 0) + int(batch.get("re_outbound_inserts") or 0)
            != EXPECTED_ACTIVE_MISSING
        ):
            conflicts += 1
    db.rollback()
    return {
        "schema_version": 1,
        "mode": "verify_counts_only",
        "safe_to_repair": conflicts == 0,
        "plan_sha256": expected_plan_sha,
        "target_audits": len(target_audits),
        "batch_audits": len(batch_audits),
        "unique_codes": len(scan_codes),
        "unique_items": len(item_ids),
        "verified_scans": verified_scans,
        "verified_movements": verified_movements,
        "verified_latest_movements": verified_latest_movements,
        "verified_code_hashes": verified_code_hashes,
        "verified_item_counters": verified_item_counters,
        "conflicts": conflicts,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--approval", default="")
    parser.add_argument("--expected-plan-sha", default="")
    parser.add_argument("--expected-active-missing", type=int, required=True)
    parser.add_argument("--observed-at", required=True)
    return parser.parse_args(argv)


def run(argv=None):
    from sqlalchemy import select, text

    args = parse_args(argv)
    if args.expected_active_missing != EXPECTED_ACTIVE_MISSING:
        raise SystemExit("ACTIVE_KIZ_EXPECTED_SCOPE_INVALID")
    observed_at = parse_observed_at(args.observed_at)
    SessionLocal = backend_module("db").SessionLocal
    OrderItem = backend_module("models").OrderItem
    lock_codes = backend_module("kiz_movements_service").lock_kiz_codes_for_transaction
    with SessionLocal() as db:
        if args.verify:
            if len(args.expected_plan_sha) != 64 or any(
                value not in "0123456789abcdef" for value in args.expected_plan_sha
            ):
                raise SystemExit("ACTIVE_KIZ_EXPECTED_PLAN_SHA_INVALID")
            result = verify_applied(db, args.expected_plan_sha, observed_at)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["safe_to_repair"] else 3
        if args.apply:
            db.execute(text("SET LOCAL lock_timeout = '15s'"))
            db.execute(text("SET LOCAL statement_timeout = '120s'"))
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), {
                "identity": "taksklad:google-cutover-active-kiz-repair:v1",
            })
        summary, candidates = create_plan(db, observed_at)
        if not summary["safe_to_repair"]:
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

        lock_codes(db, [value["code"] for value in candidates])
        item_ids = sorted({value["item"].id for value in candidates}, key=str)
        if item_ids:
            db.execute(select(OrderItem.id).where(OrderItem.id.in_(item_ids)).with_for_update()).all()
        db.expire_all()
        locked_summary, locked_candidates = create_plan(db, observed_at)
        if not locked_summary["safe_to_repair"] or locked_summary["plan_sha256"] != args.expected_plan_sha:
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
