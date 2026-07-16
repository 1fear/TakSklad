import unittest
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.google_cutover_repair import (
    apply_candidates,
    build_repair_plan,
    deterministic_uuid,
    enrich_identity_lifecycle_diagnostics,
    item_code_lifecycle_state,
    match_records_to_items,
    normalize,
    parse_returned_at,
    target_error_diagnostics,
)


UTC = timezone.utc


def order(order_id="o1", returned_at="2026-07-10T10:00:00+05:00"):
    return SimpleNamespace(
        id=order_id,
        status="returned",
        raw_payload={"return_status": "returned", "returned_at": returned_at},
    )


def item(item_id="i1", scans=None, quantity_blocks=10, scanned_blocks=0):
    return SimpleNamespace(
        id=item_id,
        order=order(),
        order_id="o1",
        product="Chapman Red OP 20",
        quantity_blocks=quantity_blocks,
        quantity_pieces=quantity_blocks * 10,
        scanned_blocks=scanned_blocks,
        scan_codes=list(scans or []),
    )


def scan(scan_id, code, scanned_at=None):
    return SimpleNamespace(
        id=scan_id,
        code=code,
        scanned_at=scanned_at or datetime(2026, 7, 9, 8, tzinfo=UTC),
        raw_payload={},
    )


def movement(movement_id, kind, *, scan_id, at, item_id="i1", order_id="o1"):
    return SimpleNamespace(
        id=movement_id,
        movement_type=kind,
        scan_code_id=scan_id,
        order_item_id=item_id,
        order_id=order_id,
        occurred_at=at,
    )


def record(code, *, returned_at="10.07.2026 10:00:00"):
    return {
        "return_status": "Возврат",
        "returned_at": returned_at,
        "scanned_codes": [code],
        "product": "Chapman Red OP 20",
        "quantity_blocks": 10,
        "quantity_pieces": 100,
    }


def plan(rows, scans_by_code=None, movements_by_code=None, identity_conflicts=0):
    return build_repair_plan(
        rows,
        scans_by_code or {},
        movements_by_code or {},
        identity_conflicts=identity_conflicts,
    )


class GoogleCutoverRepairTests(unittest.TestCase):
    def test_missing_scan_creates_exact_outbound_return_plan_and_preserves_code(self):
        code = "0104006396053947217\x1dABC"
        backend_item = item()
        summary, candidates = plan([(record(code), backend_item)])

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["missing_scan_occurrences"], 1)
        self.assertEqual(summary["scan_inserts"], 1)
        self.assertEqual(summary["outbound_inserts"], 1)
        self.assertEqual(summary["return_inserts"], 1)
        self.assertEqual(candidates[0]["code"], code)
        self.assertEqual(normalize(f"\t{code}\r\n"), code)

    def test_existing_scan_adds_only_missing_return(self):
        code = "KIZ-1"
        existing_scan = scan("s1", code)
        backend_item = item(scans=[existing_scan], scanned_blocks=1)
        outbound = movement(
            "m1", "outbound", scan_id="s1", at=datetime(2026, 7, 9, 8, tzinfo=UTC)
        )
        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [existing_scan]},
            {code: [outbound]},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["missing_return_occurrences"], 1)
        self.assertEqual(summary["scan_inserts"], 0)
        self.assertEqual(summary["outbound_inserts"], 0)
        self.assertEqual(summary["return_inserts"], 1)
        self.assertIs(candidates[0]["scan"], existing_scan)

    def test_historical_return_stays_before_later_re_outbound(self):
        code = "KIZ-2"
        old_scan = scan("s-old", code)
        new_scan = scan("s-new", code)
        backend_item = item(scans=[old_scan], scanned_blocks=1)
        outbound = movement(
            "m-out", "outbound", scan_id="s-old", at=datetime(2026, 7, 9, 8, tzinfo=UTC)
        )
        later_re_outbound = movement(
            "m-re",
            "re_outbound",
            scan_id="s-new",
            item_id="i-new",
            order_id="o-new",
            at=datetime(2026, 7, 11, 8, tzinfo=UTC),
        )
        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [old_scan, new_scan]},
            {code: [outbound, later_re_outbound]},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertLess(candidates[0]["return_at"], later_re_outbound.occurred_at)

    def test_historical_return_is_reconstructed_immediately_before_later_available(self):
        code = "KIZ-AVAILABLE-BOUNDARY"
        old_scan = scan("s-old", code)
        backend_item = item(scans=[old_scan], scanned_blocks=1)
        backend_item.order.raw_payload["returned_at"] = "2026-07-12T10:00:00+05:00"
        outbound = movement(
            "m-out", "outbound", scan_id=old_scan.id,
            at=datetime(2026, 7, 9, 8, tzinfo=UTC),
        )
        later_available = movement(
            "m-undo", "undo", scan_id="later-scan", item_id="later-item",
            order_id="later-order", at=datetime(2026, 7, 11, 8, tzinfo=UTC),
        )

        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [old_scan]},
            {code: [outbound, later_available]},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["reconstructed_chronology_occurrences"], 1)
        self.assertEqual(
            candidates[0]["return_at"],
            later_available.occurred_at - timedelta(microseconds=1),
        )
        self.assertEqual(
            candidates[0]["timestamp_provenance"],
            "reconstructed_before_available_movement",
        )

    def test_missing_scan_reconstructs_returned_previous_owner_lifecycle(self):
        code = "KIZ-BUSY-PREVIOUS"
        target = item(item_id="target-item")
        target.order = order("target-order")
        target.order_id = target.order.id
        owner_scan = scan("owner-scan", code)
        owner = item(item_id="owner-item", scans=[owner_scan], scanned_blocks=1)
        owner.order = order(
            "owner-order",
            returned_at="2026-07-09T09:00:00+00:00",
        )
        owner.order_id = owner.order.id
        owner_outbound = movement(
            "owner-out", "outbound", scan_id=owner_scan.id,
            item_id=owner.id, order_id=owner.order.id,
            at=datetime(2026, 7, 9, 8, tzinfo=UTC),
        )

        summary, candidates = build_repair_plan(
            [(record(code), target)],
            {code: [owner_scan]},
            {code: [owner_outbound]},
            relevant_items={str(owner.id): owner, str(target.id): target},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["prerequisite_return_inserts"], 1)
        self.assertEqual(summary["return_inserts"], 2)
        self.assertEqual(candidates[0]["outbound_type"], "re_outbound")
        self.assertIs(candidates[0]["prerequisite_return"]["item"], owner)
        self.assertLess(
            candidates[0]["prerequisite_return"]["return_at"],
            candidates[0]["scan_at"],
        )

    def test_legacy_scope_mismatch_blocks_before_write(self):
        summary, candidates = build_repair_plan(
            [],
            {},
            {},
            scope_diagnostics={
                "legacy_missing_scan_occurrences": 7,
                "legacy_missing_return_occurrences": 22,
                "legacy_target_occurrences": 29,
                "legacy_target_unique_codes": 28,
                "scope_conflicts": 1,
            },
        )

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["scope_conflicts"], 1)

    def test_missing_scan_without_trusted_time_uses_reconstructed_boundary(self):
        code = "KIZ-BUSY-NO-TRUSTED-TIME"
        target = item(item_id="target-item")
        target.order = order("target-order")
        owner_scan = scan("owner-scan", code)
        owner = item(item_id="owner-item", scans=[owner_scan], scanned_blocks=1)
        owner.order = order("owner-order", returned_at="")
        owner.raw_payload = {}
        owner.source_import_id = ""
        owner_outbound = movement(
            "owner-out", "outbound", scan_id=owner_scan.id,
            item_id=owner.id, order_id=owner.order.id,
            at=datetime(2026, 7, 9, 8, tzinfo=UTC),
        )

        summary, candidates = build_repair_plan(
            [(record(code), target)],
            {code: [owner_scan]},
            {code: [owner_outbound]},
            relevant_items={str(owner.id): owner},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["reconstructed_prerequisite_occurrences"], 1)
        self.assertEqual(
            candidates[0]["prerequisite_return"]["timestamp_provenance"],
            "reconstructed_boundary_before_legacy_target",
        )
        self.assertEqual(
            candidates[0]["prerequisite_return"]["return_at"],
            candidates[0]["return_at"] - timedelta(microseconds=2),
        )
        self.assertLess(
            candidates[0]["prerequisite_return"]["return_at"],
            candidates[0]["scan_at"],
        )

    def test_return_that_would_cross_later_outbound_is_blocked(self):
        code = "KIZ-3"
        old_scan = scan("s-old", code)
        new_scan = scan("s-new", code)
        backend_item = item(scans=[old_scan], scanned_blocks=1)
        backend_item.order.raw_payload["returned_at"] = "2026-07-12T10:00:00+05:00"
        movements = [
            movement("m-out", "outbound", scan_id="s-old", at=datetime(2026, 7, 9, 8, tzinfo=UTC)),
            movement(
                "m-re", "re_outbound", scan_id="s-new", item_id="i-new", order_id="o-new",
                at=datetime(2026, 7, 11, 8, tzinfo=UTC),
            ),
        ]
        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [old_scan, new_scan]},
            {code: movements},
        )

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["ambiguous_chronology"], 1)
        self.assertEqual(
            summary["target_return_crosses_later_re_outbound_other_item_occurrences"],
            1,
        )
        self.assertEqual(summary["target_return_crosses_later_movement_occurrences"], 1)

    def test_duplicate_occurrence_is_one_unique_target_but_keeps_audit_counts(self):
        code = "KIZ-4"
        backend_item = item()
        summary, candidates = plan([
            (record(code), backend_item),
            (record(code), backend_item),
        ])

        self.assertEqual(summary["missing_scan_occurrences"], 2)
        self.assertEqual(summary["missing_scan_targets"], 1)
        self.assertEqual(summary["duplicate_occurrences"], 1)
        self.assertEqual(len(candidates), 1)

    def test_duplicate_target_with_different_return_metadata_fails_closed(self):
        code = "KIZ-DUPLICATE-CONFLICT"
        first = record(code)
        first["returned_by"] = "actor-one"
        second = dict(first, returned_by="actor-two")

        summary, candidates = plan([
            (first, item()),
            (second, item()),
        ])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(summary["duplicate_occurrences"], 1)
        self.assertEqual(summary["other_conflicts"], 1)
        self.assertEqual(summary["divergent_duplicate_targets"], 1)
        self.assertFalse(summary["safe_to_repair"])

    def test_unparseable_timestamp_and_ambiguous_identity_fail_closed(self):
        backend_item = item()
        backend_item.order.raw_payload.pop("returned_at")
        summary, candidates = plan(
            [(record("KIZ-5", returned_at="10.07.2026"), backend_item)],
            identity_conflicts=1,
        )

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["unparseable_returned_at"], 1)
        self.assertEqual(summary["identity_conflicts"], 1)

    def test_identity_diagnostics_are_counts_only_and_remain_blocking(self):
        diagnostics = {
            "identity_no_strong_id_records": 1,
            "identity_not_found_records": 2,
            "identity_product_quantity_mismatch_records": 3,
            "identity_multiple_records": 4,
            "identity_order_not_returned_records": 0,
        }

        summary, candidates = build_repair_plan(
            [],
            {},
            {},
            identity_conflicts=sum(diagnostics.values()),
            identity_diagnostics=diagnostics,
        )

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["identity_conflicts"], 10)
        for field, expected in diagnostics.items():
            self.assertEqual(summary[field], expected)

    def test_ambiguous_identity_diagnostics_find_unique_scan_and_row_owner(self):
        code = "KIZ-IDENTITY-DIAGNOSTIC"
        first = SimpleNamespace(
            id="candidate-1",
            source_import_id="shared-import",
            raw_payload={
                "source_import_id": "shared-import",
                "source_order_id": "shared-order",
                "google_sheet_row_number": 42,
                "google_sheet_source_sheet": "Архив",
                "source_file": "return-source.xlsx",
                "source_row": "77",
            },
            product="Chapman Red OP 20",
            quantity_blocks=10,
            quantity_pieces=100,
            scan_codes=[scan("scan-owner", code)],
        )
        second = SimpleNamespace(
            id="candidate-2",
            source_import_id="shared-import",
            raw_payload={
                "source_import_id": "shared-import",
                "source_order_id": "other-order",
            },
            product="Chapman Red OP 20",
            quantity_blocks=10,
            quantity_pieces=100,
            scan_codes=[],
        )
        scalar_result = SimpleNamespace(all=lambda: [first, second])
        db = SimpleNamespace(execute=lambda _statement: SimpleNamespace(scalars=lambda: scalar_result))
        google_record = {
            **record(code),
            "source_import_id": "shared-import",
            "source_order_id": "shared-order",
            "source_file": "return-source.xlsx",
            "source_row": "77",
            "row_number": 42,
            "source_sheet": "Архив",
        }

        matched, diagnostics, contexts, items_by_id = match_records_to_items(db, [google_record])

        self.assertIsNone(matched[0][1])
        self.assertEqual(diagnostics["identity_multiple_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_scan_owner_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_row_owner_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_both_source_ids_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_source_file_row_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_signal_agreement_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_single_unique_signal_records"], 0)
        self.assertEqual(diagnostics["identity_multiple_signal_conflict_records"], 0)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(len(items_by_id), 2)
        self.assertEqual(
            diagnostics["identity_multiple_codes_without_candidate_scan_occurrences"],
            0,
        )

    def test_ambiguous_identity_one_signal_is_not_reported_as_agreement(self):
        code = "KIZ-ONE-IDENTITY-SIGNAL"
        first = SimpleNamespace(
            id="candidate-1",
            source_import_id="shared-import",
            raw_payload={"source_import_id": "shared-import"},
            product="Chapman Red OP 20",
            quantity_blocks=10,
            quantity_pieces=100,
            scan_codes=[scan("scan-owner", code)],
        )
        second = SimpleNamespace(
            id="candidate-2",
            source_import_id="shared-import",
            raw_payload={"source_import_id": "shared-import"},
            product="Chapman Red OP 20",
            quantity_blocks=10,
            quantity_pieces=100,
            scan_codes=[],
        )
        scalar_result = SimpleNamespace(all=lambda: [first, second])
        db = SimpleNamespace(execute=lambda _statement: SimpleNamespace(scalars=lambda: scalar_result))
        google_record = {**record(code), "source_import_id": "shared-import"}

        _matched, diagnostics, contexts, items_by_id = match_records_to_items(db, [google_record])

        self.assertEqual(diagnostics["identity_multiple_single_unique_signal_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_signal_agreement_records"], 0)
        self.assertEqual(diagnostics["identity_multiple_signal_conflict_records"], 0)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(len(items_by_id), 2)

    def test_identity_lifecycle_diagnostics_distinguish_source_and_row_candidates(self):
        code = "KIZ-LIFECYCLE-DIAGNOSTIC"
        source_order = order("order-source")
        row_order = order("order-row")
        source_scan = scan("scan-source", code)
        row_scan = scan("scan-row", code)
        source_candidate = SimpleNamespace(
            id="item-source",
            order=source_order,
            source_import_id="shared-import",
            raw_payload={"source_order_id": "source-order"},
            scan_codes=[source_scan],
        )
        row_candidate = SimpleNamespace(
            id="item-row",
            order=row_order,
            source_import_id="shared-import",
            raw_payload={
                "source_order_id": "other-order",
                "google_sheet_row_number": 42,
                "google_sheet_source_sheet": "Архив",
            },
            scan_codes=[row_scan],
        )
        google_record = {
            **record(code),
            "source_import_id": "shared-import",
            "source_order_id": "source-order",
            "row_number": 42,
            "source_sheet": "Архив",
        }
        movements_by_code = {
            code: [
                movement(
                    "source-out",
                    "outbound",
                    scan_id=source_scan.id,
                    item_id=source_candidate.id,
                    order_id=source_order.id,
                    at=datetime(2026, 7, 9, 8, tzinfo=UTC),
                ),
                movement(
                    "row-out",
                    "outbound",
                    scan_id=row_scan.id,
                    item_id=row_candidate.id,
                    order_id=row_order.id,
                    at=datetime(2026, 7, 10, 9, tzinfo=UTC),
                ),
                movement(
                    "source-return",
                    "return",
                    scan_id=source_scan.id,
                    item_id=source_candidate.id,
                    order_id=source_order.id,
                    at=datetime(2026, 7, 10, 8, tzinfo=UTC),
                ),
            ]
        }
        context = {
            "record": google_record,
            "candidates": [source_candidate, row_candidate],
            "import_candidates": [source_candidate, row_candidate],
            "order_candidates": [source_candidate],
        }
        diagnostics = defaultdict(int)

        enrich_identity_lifecycle_diagnostics(diagnostics, [context], movements_by_code)

        self.assertEqual(item_code_lifecycle_state(source_candidate, code, movements_by_code), "complete")
        self.assertEqual(item_code_lifecycle_state(row_candidate, code, movements_by_code), "missing_return")
        self.assertEqual(
            diagnostics["identity_multiple_exactly_one_candidate_missing_return_records"],
            1,
        )
        self.assertEqual(
            diagnostics["identity_multiple_source_ids_complete_row_missing_records"],
            1,
        )

        overlapping = movement(
            "later-re-outbound",
            "re_outbound",
            scan_id="later-scan",
            item_id="later-item",
            order_id="later-order",
            at=datetime(2026, 7, 11, 9, tzinfo=UTC),
        )
        self.assertEqual(
            item_code_lifecycle_state(
                row_candidate,
                code,
                {code: [*movements_by_code[code], overlapping]},
            ),
            "invalid",
        )

    def test_target_error_diagnostics_use_owner_and_trusted_interval_counts(self):
        code = "KIZ-TARGET-DIAGNOSTIC"
        target = item(item_id="target-item")
        owner_scan = scan("owner-scan", code)
        owner = item(item_id="owner-item", scans=[owner_scan], scanned_blocks=1)
        owner.source_import_id = "shared-import"
        owner.raw_payload = {
            "source_order_id": "shared-order",
            "google_sheet_row_number": 42,
            "google_sheet_source_sheet": "Архив",
        }
        owner.order.raw_payload["returned_at"] = "2026-07-10T08:00:00+00:00"
        owner_outbound = movement(
            "owner-out",
            "outbound",
            scan_id=owner_scan.id,
            item_id=owner.id,
            order_id=owner.order.id,
            at=datetime(2026, 7, 9, 8, tzinfo=UTC),
        )
        google_record = {
            **record(code, returned_at="10.07.2026 10:00:00"),
            "source_import_id": "shared-import",
            "source_order_id": "shared-order",
            "row_number": 42,
            "source_sheet": "Архив",
        }
        busy = target_error_diagnostics(
            target,
            google_record,
            code,
            "busy_before_missing_scan",
            {code: [owner_outbound]},
            relevant_items={str(owner.id): owner},
        )
        self.assertEqual(busy["busy_previous_outbound_occurrences"], 1)
        self.assertEqual(busy["busy_previous_owner_matches_both_source_ids_occurrences"], 1)
        self.assertEqual(busy["busy_previous_owner_scan_matches_movement_occurrences"], 1)
        empty_provenance = target_error_diagnostics(
            target,
            {**google_record, "row_number": 0, "source_sheet": ""},
            code,
            "busy_before_missing_scan",
            {code: [owner_outbound]},
            relevant_items={str(owner.id): owner},
        )
        self.assertEqual(
            empty_provenance["busy_previous_owner_matches_google_row_occurrences"],
            0,
        )

        target.scan_codes = [owner_scan]
        target.id = owner.id
        target.order = owner.order
        next_undo = movement(
            "next-undo",
            "undo",
            scan_id="another-scan",
            item_id="another-item",
            order_id="another-order",
            at=datetime(2026, 7, 11, 8, tzinfo=UTC),
        )
        crossed = target_error_diagnostics(
            target,
            {**google_record, "returned_at": "12.07.2026 10:00:00"},
            code,
            "target_return_crosses_later_available_movement",
            {code: [owner_outbound, next_undo]},
            audit_return_times={
                str(owner.order.id): [datetime(2026, 7, 10, 8, tzinfo=UTC)]
            },
        )
        self.assertEqual(crossed["cross_later_backend_timestamp_in_interval_occurrences"], 1)
        self.assertEqual(crossed["cross_later_unique_audit_timestamp_in_interval_occurrences"], 1)
        self.assertEqual(
            crossed["cross_later_one_unique_trusted_timestamp_in_interval_occurrences"],
            1,
        )
        self.assertEqual(crossed["cross_later_all_trusted_timestamps_agree_occurrences"], 1)

    def test_oversized_return_metadata_fails_closed_before_database_write(self):
        oversized = record("KIZ-LONG")
        oversized["return_reference"] = "x" * 121
        summary, candidates = plan([
            (oversized, item()),
        ])

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["other_conflicts"], 1)
        self.assertEqual(summary["return_reference_too_long_occurrences"], 1)

    def test_existing_outbound_for_other_item_fails_closed(self):
        code = "KIZ-WRONG-OWNER"
        existing_scan = scan("s-wrong", code)
        backend_item = item(scans=[existing_scan], scanned_blocks=1)
        wrong_outbound = movement(
            "m-wrong",
            "outbound",
            scan_id=existing_scan.id,
            item_id="another-item",
            order_id="another-order",
            at=datetime(2026, 7, 9, 8, tzinfo=UTC),
        )

        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [existing_scan]},
            {code: [wrong_outbound]},
        )

        self.assertEqual(candidates, [])
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["other_conflicts"], 1)
        self.assertEqual(summary["outbound_owner_mismatch_occurrences"], 1)

    def test_preexisting_return_anomaly_is_counted_but_outside_repair_scope(self):
        code = "KIZ-BAD-RETURN"
        existing_scan = scan("s-bad-return", code)
        backend_item = item(scans=[existing_scan], scanned_blocks=1)
        movements = [
            movement(
                "m-out",
                "outbound",
                scan_id=existing_scan.id,
                at=datetime(2026, 7, 10, 8, tzinfo=UTC),
            ),
            movement(
                "m-return",
                "return",
                scan_id=existing_scan.id,
                at=datetime(2026, 7, 10, 7, tzinfo=UTC),
            ),
        ]

        summary, candidates = plan(
            [(record(code), backend_item)],
            {code: [existing_scan]},
            {code: movements},
        )

        self.assertEqual(candidates, [])
        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["already_repaired_occurrences"], 1)
        self.assertEqual(summary["preexisting_anomaly_occurrences"], 1)

    def test_plan_hash_covers_mutation_metadata(self):
        code = "KIZ-HASH"
        first_record = record(code)
        second_record = dict(first_record, returned_by="another-actor")

        first_summary, _ = plan([(first_record, item())])
        second_summary, _ = plan([(second_record, item())])

        self.assertNotEqual(first_summary["plan_sha256"], second_summary["plan_sha256"])

    def test_timestamp_parsing_requires_time_and_uses_tashkent_for_legacy_format(self):
        backend_order = order(returned_at="")
        parsed, provenance = parse_returned_at(
            backend_order,
            {"returned_at": "10.07.2026 10:00:00"},
        )

        self.assertEqual(parsed, datetime(2026, 7, 10, 5, tzinfo=UTC))
        self.assertEqual(provenance, "google_sheet")
        self.assertEqual(parse_returned_at(backend_order, {"returned_at": "10.07.2026"}), (None, ""))

    def test_deterministic_ids_are_stable_and_target_specific(self):
        first = deterministic_uuid("movement", "scan-1", "return")
        second = deterministic_uuid("movement", "scan-1", "return")
        other = deterministic_uuid("movement", "scan-2", "return")

        self.assertIsInstance(first, uuid.UUID)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_apply_is_idempotent_and_creates_outbound_then_return(self):
        from backend.app.models import AuditLog, Base, KizMovement, Order, OrderItem, ScanCode

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        order_id = uuid.uuid4()
        item_id = uuid.uuid4()
        with Session(engine) as db:
            backend_order = Order(
                id=order_id,
                payment_type="terminal",
                client="synthetic",
                address="synthetic",
                status="returned",
                raw_payload={"return_status": "returned"},
            )
            backend_item = OrderItem(
                id=item_id,
                order=backend_order,
                product="synthetic",
                quantity_pieces=10,
                quantity_blocks=1,
                scanned_blocks=0,
                requires_kiz=True,
                status="completed",
                raw_payload={},
            )
            db.add(backend_order)
            db.flush()
            scan_at = datetime(2026, 7, 10, 4, 59, 59, 999999, tzinfo=UTC)
            return_at = datetime(2026, 7, 10, 5, tzinfo=UTC)
            candidate = {
                "kind": "missing_scan",
                "code": "SYNTHETIC-KIZ",
                "record": {"return_reference": "synthetic", "returned_by": "test"},
                "item": backend_item,
                "scan": None,
                "outbound_type": "outbound",
                "scan_at": scan_at,
                "return_at": return_at,
                "original_return_at": return_at,
                "timestamp_provenance": "backend_order",
                "timestamp_adjusted": False,
                "new_scanned_blocks": 1,
            }
            summary = {
                "plan_sha256": "a" * 64,
                "scan_inserts": 1,
                "outbound_inserts": 1,
                "return_inserts": 1,
            }
            first = apply_candidates(db, [candidate], summary)

            self.assertEqual(first["scan_inserts"], 1)
            self.assertEqual(first["outbound_inserts"], 1)
            self.assertEqual(first["return_inserts"], 1)
            movements = db.execute(select(KizMovement).order_by(KizMovement.occurred_at)).scalars().all()
            self.assertEqual([row.movement_type for row in movements], ["outbound", "return"])
            self.assertEqual(db.execute(select(ScanCode)).scalars().all()[0].code, "SYNTHETIC-KIZ")
            self.assertEqual(len(db.execute(select(AuditLog)).scalars().all()), 1)

            second = apply_candidates(db, [], {
                "plan_sha256": summary["plan_sha256"],
                "scan_inserts": 0,
                "outbound_inserts": 0,
                "return_inserts": 0,
            })
            self.assertEqual(second["mutations_applied"], 0)
            self.assertEqual(len(db.execute(select(ScanCode)).scalars().all()), 1)
            self.assertEqual(len(db.execute(select(KizMovement)).scalars().all()), 2)
            self.assertEqual(len(db.execute(select(AuditLog)).scalars().all()), 1)

    def test_apply_inserts_prerequisite_return_before_re_outbound(self):
        from backend.app.models import (
            Base,
            KizCode,
            KizMovement,
            Order,
            OrderItem,
            ScanCode,
        )

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            owner_order = Order(
                id=uuid.uuid4(), payment_type="terminal", client="synthetic-owner",
                address="synthetic", status="returned", raw_payload={"return_status": "returned"},
            )
            target_order = Order(
                id=uuid.uuid4(), payment_type="terminal", client="synthetic-target",
                address="synthetic", status="returned", raw_payload={"return_status": "returned"},
            )
            owner_item = OrderItem(
                id=uuid.uuid4(), order=owner_order, product="synthetic",
                quantity_pieces=10, quantity_blocks=1, scanned_blocks=1,
                requires_kiz=True, status="completed", raw_payload={},
            )
            target_item = OrderItem(
                id=uuid.uuid4(), order=target_order, product="synthetic",
                quantity_pieces=10, quantity_blocks=1, scanned_blocks=0,
                requires_kiz=True, status="completed", raw_payload={},
            )
            owner_scan = ScanCode(
                id=uuid.uuid4(), order_item_id=owner_item.id, code="PREREQUISITE-KIZ",
                source="test", scanned_at=datetime(2026, 7, 9, 7, tzinfo=UTC), raw_payload={},
            )
            kiz = KizCode(id=uuid.uuid4(), code="PREREQUISITE-KIZ")
            owner_outbound = KizMovement(
                id=uuid.uuid4(), kiz_id=kiz.id, movement_type="outbound",
                order_id=owner_order.id, order_item_id=owner_item.id,
                scan_code_id=owner_scan.id, source="test", actor="test",
                occurred_at=datetime(2026, 7, 9, 8, tzinfo=UTC), raw_payload={},
            )
            db.add_all([owner_order, target_order, owner_item, target_item, owner_scan, kiz, owner_outbound])
            db.flush()
            prerequisite_at = datetime(2026, 7, 9, 8, tzinfo=UTC) + timedelta(microseconds=1)
            scan_at = datetime(2026, 7, 10, 4, 59, 59, 999999, tzinfo=UTC)
            return_at = datetime(2026, 7, 10, 5, tzinfo=UTC)
            candidate = {
                "kind": "missing_scan", "code": kiz.code, "record": {},
                "item": target_item, "scan": None, "outbound_type": "re_outbound",
                "scan_at": scan_at, "return_at": return_at,
                "original_return_at": return_at, "timestamp_provenance": "backend_order",
                "timestamp_adjusted": False, "new_scanned_blocks": 1,
                "prerequisite_return": {
                    "item": owner_item, "scan": owner_scan,
                    "outbound": owner_outbound, "return_at": prerequisite_at,
                    "timestamp_provenance": "backend_order",
                },
            }

            result = apply_candidates(db, [candidate], {
                "plan_sha256": "c" * 64,
                "scan_inserts": 1,
                "outbound_inserts": 1,
                "return_inserts": 2,
                "prerequisite_return_inserts": 1,
            })

            self.assertEqual(result["mutations_applied"], 4)
            movements = db.execute(
                select(KizMovement).order_by(KizMovement.occurred_at)
            ).scalars().all()
            self.assertEqual(
                [row.movement_type for row in movements],
                ["outbound", "return", "re_outbound", "return"],
            )

    def test_commit_failure_rolls_back_every_repair_row(self):
        from backend.app.models import Base, KizMovement, Order, OrderItem, ScanCode

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        order_id = uuid.uuid4()
        item_id = uuid.uuid4()
        try:
            with Session(engine) as db:
                backend_order = Order(
                    id=order_id,
                    payment_type="terminal",
                    client="synthetic",
                    address="synthetic",
                    status="returned",
                    raw_payload={"return_status": "returned"},
                )
                backend_item = OrderItem(
                    id=item_id,
                    order=backend_order,
                    product="synthetic",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=0,
                    requires_kiz=True,
                    status="completed",
                    raw_payload={},
                )
                db.add(backend_order)
                db.flush()
                return_at = datetime(2026, 7, 10, 5, tzinfo=UTC)
                candidate = {
                    "kind": "missing_scan",
                    "code": "ROLLBACK-KIZ",
                    "record": {},
                    "item": backend_item,
                    "scan": None,
                    "outbound_type": "outbound",
                    "scan_at": return_at - timedelta(microseconds=1),
                    "return_at": return_at,
                    "original_return_at": return_at,
                    "timestamp_provenance": "backend_order",
                    "timestamp_adjusted": False,
                    "new_scanned_blocks": 1,
                }
                with mock.patch.object(db, "commit", side_effect=RuntimeError("synthetic commit failure")):
                    apply_candidates(db, [candidate], {
                        "plan_sha256": "b" * 64,
                        "scan_inserts": 1,
                        "outbound_inserts": 1,
                        "return_inserts": 1,
                    })
        except RuntimeError:
            pass
        else:
            self.fail("commit failure was not propagated")

        with Session(engine) as verify:
            self.assertEqual(verify.execute(select(ScanCode)).scalars().all(), [])
            self.assertEqual(verify.execute(select(KizMovement)).scalars().all(), [])


if __name__ == "__main__":
    unittest.main()
