import unittest
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, ImportJob, Order, OrderItem, PendingEvent, ScanCode
from backend.app.transfer_kiz_service import (
    TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
    TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE,
    process_transfer_kiz_completion_check,
    queue_transfer_kiz_client_delivery,
    queue_transfer_kiz_undo_alert,
    transfer_kiz_source_key,
)


class TransferKizServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_completion_gates_block_delivery(self):
        cases = {
            "import_has_skipped_or_invalid_rows": {"skipped_rows_count": 1},
            "non_transfer_payment_group": {"payment_type": "Терминал"},
            "missing_skladbot_request_number": {"skladbot_request_number": ""},
            "incomplete_kiz_items": {"scanned_blocks": 0},
        }
        for expected, options in cases.items():
            with self.subTest(expected=expected):
                event_id = self._seed_ready_check(**options)
                with self.SessionLocal() as db:
                    result = process_transfer_kiz_completion_check(db, db.get(PendingEvent, event_id))
                    db.commit()
                    deliveries = db.execute(
                        select(PendingEvent).where(
                            PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE
                        )
                    ).scalars().all()
                self.assertEqual(result["status"], "not_ready")
                self.assertIn(expected, result["blockers"])
                self.assertEqual(deliveries, [])
                self._clear_database()

    def test_non_kiz_scan_does_not_satisfy_required_kiz_scan_gate(self):
        event_id = self._seed_ready_check(include_kiz_scan=False)
        with self.SessionLocal() as db:
            kiz_item = db.execute(select(OrderItem).where(OrderItem.requires_kiz.is_(True))).scalar_one()
            non_kiz_item = OrderItem(
                id=uuid.uuid4(),
                order_id=kiz_item.order_id,
                product="Synthetic non-KIZ product",
                quantity_pieces=1,
                quantity_blocks=0,
                scanned_blocks=0,
                requires_kiz=False,
                status="completed",
                raw_payload=dict(kiz_item.raw_payload),
            )
            non_kiz_scan = ScanCode(
                id=uuid.uuid4(),
                order_item_id=non_kiz_item.id,
                code=f"01{uuid.uuid4().hex}",
                source="desktop",
            )
            event = db.get(PendingEvent, event_id)
            event.payload = {"scan_id": str(non_kiz_scan.id)}
            db.add_all([non_kiz_item, non_kiz_scan])
            db.flush()

            result = process_transfer_kiz_completion_check(db, event)
            db.commit()
            deliveries = db.execute(
                select(PendingEvent).where(
                    PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE
                )
            ).scalars().all()

        self.assertEqual(result["status"], "not_ready")
        self.assertIn("no_scans", result["blockers"])
        self.assertEqual(deliveries, [])

    def test_completion_replay_creates_exactly_one_delivery(self):
        event_id = self._seed_ready_check()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            first = process_transfer_kiz_completion_check(db, event)
            second = process_transfer_kiz_completion_check(db, event)
            db.commit()
            deliveries = db.execute(
                select(PendingEvent).where(
                    PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE
                )
            ).scalars().all()

        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "ready")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].payload["source_key"], first["source_key"])

    def test_same_filename_from_two_imports_has_two_distinct_source_keys(self):
        first_event_id = self._seed_ready_check(source_file="orders.xlsx")
        second_event_id = self._seed_ready_check(source_file="orders.xlsx")
        with self.SessionLocal() as db:
            first = process_transfer_kiz_completion_check(db, db.get(PendingEvent, first_event_id))
            second = process_transfer_kiz_completion_check(db, db.get(PendingEvent, second_event_id))
            db.commit()
            deliveries = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE)
                .order_by(PendingEvent.created_at, PendingEvent.id)
            ).scalars().all()

        self.assertNotEqual(first["source_key"], second["source_key"])
        self.assertEqual(len(deliveries), 2)
        self.assertEqual({event.payload["source_key"] for event in deliveries}, {first["source_key"], second["source_key"]})

    def test_undo_after_completed_delivery_queues_one_generic_admin_alert(self):
        event_id = self._seed_ready_check()
        with self.SessionLocal() as db:
            result = process_transfer_kiz_completion_check(db, db.get(PendingEvent, event_id))
            delivery = db.execute(
                select(PendingEvent).where(PendingEvent.id == uuid.UUID(result["delivery_event_id"]))
            ).scalar_one()
            delivery.status = "completed"
            db.flush()
            item = db.execute(select(OrderItem)).scalar_one()
            first = queue_transfer_kiz_undo_alert(db, item=item)
            second = queue_transfer_kiz_undo_alert(db, item=item)
            first_id = first.id
            second_id = second.id
            alert_kind = first.payload["kind"]
            alert_text = first.payload["text"]
            db.commit()

        self.assertEqual(first_id, second_id)
        self.assertEqual(alert_kind, "daily_reconciliation_alert")
        self.assertNotIn("import:", alert_text)

    def _seed_ready_check(
        self,
        *,
        source_file="orders.xlsx",
        payment_type="Перечисление",
        skladbot_request_number="SB-1",
        skipped_rows_count=0,
        invalid_rows=0,
        scanned_blocks=1,
        include_kiz_scan=True,
    ):
        import_id = uuid.uuid4()
        order_id = uuid.uuid4()
        item_id = uuid.uuid4()
        scan_id = uuid.uuid4()
        event_id = uuid.uuid4()
        with self.SessionLocal() as db:
            import_job = ImportJob(
                id=import_id,
                source="telegram",
                status="completed",
                rows_total=1,
                rows_imported=1,
                raw_payload={
                    "skipped_rows_count": skipped_rows_count,
                    "invalid_rows": invalid_rows,
                },
            )
            order = Order(
                id=order_id,
                source="telegram",
                payment_type=payment_type,
                client="Synthetic client",
                address="Synthetic address",
                status="not_completed",
                raw_payload={"skladbot_request_number": skladbot_request_number},
            )
            item = OrderItem(
                id=item_id,
                order_id=order_id,
                product="Synthetic product",
                quantity_pieces=10,
                quantity_blocks=2 if not scanned_blocks else 1,
                scanned_blocks=scanned_blocks,
                requires_kiz=True,
                status="completed" if scanned_blocks else "not_completed",
                raw_payload={"backend_import_id": str(import_id), "source_file": source_file},
            )
            scan = ScanCode(
                id=scan_id,
                order_item_id=item_id,
                code=f"01{scan_id.hex}",
                source="desktop",
            )
            event = PendingEvent(
                id=event_id,
                event_type=TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE,
                action="check_completion",
                aggregate_type="scan_code",
                aggregate_id=str(scan_id),
                idempotency_key=f"transfer-kiz:test:{event_id}",
                status="pending",
                payload={"scan_id": str(scan_id)},
            )
            db.add_all([import_job, order, item, event])
            if include_kiz_scan:
                db.add(scan)
            db.commit()
        return event_id

    def _clear_database(self):
        with self.SessionLocal() as db:
            for model in (PendingEvent, ScanCode, OrderItem, Order, ImportJob):
                db.query(model).delete()
            db.commit()


if __name__ == "__main__":
    unittest.main()
