import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tools.google_cutover_repair import (
    apply_candidates,
    build_repair_plan,
    deterministic_uuid,
    match_records_to_items,
    normalize,
    parse_returned_at,
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

        matched, diagnostics = match_records_to_items(db, [google_record])

        self.assertIsNone(matched[0][1])
        self.assertEqual(diagnostics["identity_multiple_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_scan_owner_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_row_owner_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_both_source_ids_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_unique_source_file_row_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_signal_agreement_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_single_unique_signal_records"], 0)
        self.assertEqual(diagnostics["identity_multiple_signal_conflict_records"], 0)
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

        _matched, diagnostics = match_records_to_items(db, [google_record])

        self.assertEqual(diagnostics["identity_multiple_single_unique_signal_records"], 1)
        self.assertEqual(diagnostics["identity_multiple_signal_agreement_records"], 0)
        self.assertEqual(diagnostics["identity_multiple_signal_conflict_records"], 0)

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
