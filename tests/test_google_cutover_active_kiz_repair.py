import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.google_cutover_active_kiz_repair import (
    apply_candidates,
    build_plan,
    lifecycle_conflict,
    strong_candidates,
    verify_applied,
)


OBSERVED_AT = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
UNIT_PREFIX = "0104006396053978"


def ns(**values):
    return SimpleNamespace(**values)


def make_item(index, *, quantity_blocks=10, scanned_blocks=0, status="not_completed", scans=None):
    order = ns(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"order-{index}"),
        status="not_completed",
        created_at=OBSERVED_AT - timedelta(days=2),
        updated_at=OBSERVED_AT - timedelta(days=1),
    )
    return ns(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"item-{index}"),
        order=order,
        order_id=order.id,
        product="Chapman Brown OP 20",
        quantity_blocks=quantity_blocks,
        quantity_pieces=quantity_blocks * 10,
        scanned_blocks=scanned_blocks,
        status=status,
        requires_kiz=True,
        scan_codes=list(scans or []),
        created_at=OBSERVED_AT - timedelta(days=2),
        updated_at=OBSERVED_AT - timedelta(days=1),
    )


def make_targets(count=6, *, shared_item=None):
    result = []
    for index in range(count):
        item = shared_item or make_item(index)
        code = f"{UNIT_PREFIX}21ACTIVE{index:02d}"
        result.append(({
            "source_import_id": f"IMPORT-{index}",
            "source_order_id": f"ORDER-{index}",
            "source_sheet": "Orders",
            "row_number": index + 2,
        }, item, [code]))
    return result


def make_movement(index, movement_type, *, scan_id, order_id, item_id, occurred_at):
    return ns(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"movement-{index}-{movement_type}"),
        movement_type=movement_type,
        order_id=order_id,
        order_item_id=item_id,
        scan_code_id=scan_id,
        occurred_at=occurred_at,
    )


def plan(targets, scans=None, movements=None, registered=None):
    return build_plan(
        targets,
        scans or {},
        movements or {},
        registered or set(),
        {"active_records_total": 6},
        observed_at=OBSERVED_AT,
        snapshot_sha="a" * 64,
    )


class GoogleCutoverActiveKizRepairTests(unittest.TestCase):
    def test_exact_six_clean_codes_are_safe(self):
        summary, candidates = plan(make_targets())

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["active_missing_code_occurrences"], 6)
        self.assertEqual(summary["active_missing_unique_codes"], 6)
        self.assertEqual(summary["active_missing_unique_item_codes"], 6)
        self.assertEqual(summary["scan_inserts"], 6)
        self.assertEqual(summary["outbound_inserts"], 6)
        self.assertEqual(summary["re_outbound_inserts"], 0)
        self.assertEqual(summary["kiz_code_inserts"], 6)
        self.assertEqual(len(candidates), 6)

    def test_six_occurrences_with_five_unique_codes_are_blocked(self):
        targets = make_targets()
        targets[-1] = (targets[-1][0], targets[-1][1], [targets[0][2][0]])

        summary, _ = plan(targets)

        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["active_missing_code_occurrences"], 6)
        self.assertEqual(summary["active_missing_unique_codes"], 5)
        self.assertEqual(summary["scope_conflicts"], 1)

    def test_latest_return_produces_re_outbound(self):
        targets = make_targets()
        code = targets[0][2][0]
        old_item = make_item("old")
        scan_id = uuid.uuid5(uuid.NAMESPACE_URL, "old-scan")
        old_scan = ns(id=scan_id, order_item_id=old_item.id, code=code, scanned_at=OBSERVED_AT - timedelta(days=2), raw_payload={})
        outbound = make_movement(
            1,
            "outbound",
            scan_id=scan_id,
            order_id=old_item.order.id,
            item_id=old_item.id,
            occurred_at=OBSERVED_AT - timedelta(days=2),
        )
        returned = make_movement(
            2,
            "return",
            scan_id=scan_id,
            order_id=old_item.order.id,
            item_id=old_item.id,
            occurred_at=OBSERVED_AT - timedelta(days=1),
        )

        summary, _ = plan(
            targets,
            {code: [old_scan]},
            {code: [outbound, returned]},
            {code},
        )

        self.assertTrue(summary["safe_to_repair"])
        self.assertEqual(summary["re_outbound_inserts"], 1)
        self.assertEqual(summary["outbound_inserts"], 5)
        self.assertEqual(summary["kiz_code_inserts"], 5)

    def test_busy_malformed_and_orphan_lifecycles_are_blocked(self):
        for case in ("busy", "malformed", "orphan"):
            with self.subTest(case=case):
                targets = make_targets()
                code = targets[0][2][0]
                item = targets[0][1]
                scan_id = uuid.uuid4()
                registered = {code}
                movements = {}
                if case == "busy":
                    movements[code] = [make_movement(
                        1, "outbound", scan_id=scan_id, order_id=item.order.id,
                        item_id=item.id, occurred_at=OBSERVED_AT - timedelta(days=1),
                    )]
                elif case == "malformed":
                    movements[code] = [make_movement(
                        1, "return", scan_id=scan_id, order_id=item.order.id,
                        item_id=item.id, occurred_at=OBSERVED_AT - timedelta(days=1),
                    )]
                summary, _ = plan(targets, movements=movements, registered=registered)
                self.assertFalse(summary["safe_to_repair"])

    def test_counter_status_and_capacity_inconsistencies_are_blocked(self):
        cases = [
            make_item("counter", scanned_blocks=1),
            make_item("status", status="completed"),
            make_item("capacity", quantity_blocks=1),
        ]
        for index, item in enumerate(cases):
            with self.subTest(index=index):
                targets = make_targets(shared_item=item)
                summary, _ = plan(targets)
                self.assertFalse(summary["safe_to_repair"])

    def test_unknown_or_mismatched_product_is_blocked(self):
        targets = make_targets()
        targets[0][1].product = "Unknown product"
        summary, _ = plan(targets)
        self.assertFalse(summary["safe_to_repair"])
        self.assertEqual(summary["unknown_product_occurrences"], 1)

    def test_plan_hash_covers_observed_time_and_full_history(self):
        targets = make_targets()
        first, _ = plan(targets)
        second, _ = build_plan(
            targets, {}, {}, set(), {"active_records_total": 6},
            observed_at=OBSERVED_AT + timedelta(seconds=1), snapshot_sha="a" * 64,
        )
        third, _ = build_plan(
            targets, {}, {}, set(), {"active_records_total": 6},
            observed_at=OBSERVED_AT, snapshot_sha="b" * 64,
        )
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertNotEqual(first["plan_sha256"], third["plan_sha256"])

    def test_two_strong_ids_must_resolve_to_same_unique_item(self):
        first = make_item("first")
        second = make_item("second")
        record = {
            "source_import_id": "IMPORT",
            "source_order_id": "ORDER",
            "product": first.product,
            "quantity_blocks": first.quantity_blocks,
            "quantity_pieces": first.quantity_pieces,
        }
        self.assertEqual(strong_candidates(record, {"IMPORT": [first]}, {"ORDER": [second]}), [])

    def test_lifecycle_rejects_equal_timestamps(self):
        item = make_item("history")
        scan_id = uuid.uuid4()
        when = OBSERVED_AT - timedelta(days=1)
        movements = [
            make_movement(1, "outbound", scan_id=scan_id, order_id=item.order.id, item_id=item.id, occurred_at=when),
            make_movement(2, "return", scan_id=scan_id, order_id=item.order.id, item_id=item.id, occurred_at=when),
        ]
        self.assertEqual(lifecycle_conflict(movements, ""), "malformed")

    def test_apply_and_independent_verifier_are_exact_and_duplicate_safe(self):
        from sqlalchemy import create_engine, func, select
        from sqlalchemy.orm import sessionmaker

        from backend.app.models import Base, KizMovement, Order, OrderItem, ScanCode

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as db:
            targets = []
            for index in range(6):
                order = Order(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, f"db-order-{index}"),
                    payment_type="Terminal",
                    client=f"Client {index}",
                    address="Warehouse",
                    status="not_completed",
                    created_at=OBSERVED_AT - timedelta(days=2),
                    updated_at=OBSERVED_AT - timedelta(days=1),
                )
                item = OrderItem(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, f"db-item-{index}"),
                    order=order,
                    product="Chapman Brown OP 20",
                    quantity_blocks=10,
                    quantity_pieces=100,
                    scanned_blocks=0,
                    requires_kiz=True,
                    status="not_completed",
                    created_at=OBSERVED_AT - timedelta(days=2),
                    updated_at=OBSERVED_AT - timedelta(days=1),
                )
                db.add(order)
                db.add(item)
                targets.append(({
                    "source_import_id": f"DB-IMPORT-{index}",
                    "source_order_id": f"DB-ORDER-{index}",
                    "source_sheet": "Orders",
                    "row_number": index + 2,
                }, item, [f"{UNIT_PREFIX}21DBACTIVE{index:02d}"]))
            db.flush()
            summary, candidates = build_plan(
                targets, {}, {}, set(), {"active_records_total": 6},
                observed_at=OBSERVED_AT, snapshot_sha="c" * 64,
            )
            result = apply_candidates(db, candidates, summary)
            self.assertEqual(result["mutations_applied"], 12)
            self.assertEqual(result["target_audit_inserts"], 6)

            verified = verify_applied(db, summary["plan_sha256"], OBSERVED_AT)
            self.assertTrue(verified["safe_to_repair"], verified)
            self.assertEqual(verified["verified_scans"], 6)
            self.assertEqual(verified["verified_movements"], 6)
            self.assertEqual(verified["verified_latest_movements"], 6)
            self.assertEqual(verified["verified_item_counters"], 6)

            with self.assertRaises(RuntimeError):
                apply_candidates(db, candidates, summary)
            db.rollback()
            self.assertEqual(db.scalar(select(func.count()).select_from(ScanCode)), 6)
            self.assertEqual(db.scalar(select(func.count()).select_from(KizMovement)), 6)


if __name__ == "__main__":
    unittest.main()
