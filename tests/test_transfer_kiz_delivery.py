import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, ImportJob, Order, OrderItem, PendingEvent, ScanCode
from backend.app.telegram_clients import TelegramProcessorPorts
from backend.app.telegram_transfer_kiz_processor import TelegramTransferKizProcessor
from backend.app.transfer_kiz_service import (
    TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
    transfer_kiz_source_key,
)


class TransferKizDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.sent = []
        ports = TelegramProcessorPorts(session_factory=self.SessionLocal, token="synthetic")
        self.processor = TelegramTransferKizProcessor(ports=ports)
        self.processor.send_document = lambda chat_id, content, filename, caption="": self.sent.append(
            (chat_id, content, filename, caption)
        )
        self.client_env = {"SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": "-100123"}

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_happy_path_sends_exactly_once_to_configured_client_with_contract_caption(self):
        self._seed_delivery()
        with mock.patch.dict(os.environ, self.client_env, clear=False):
            self.processor.process_pending_transfer_kiz_deliveries()
            self.processor.process_pending_transfer_kiz_deliveries()

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "-100123")
        self.assertEqual(self.sent[0][2], "TakSklad_КИЗ_orders.xlsx")
        self.assertEqual(self.sent[0][3], "Коды маркировки по файлу: orders.xlsx")
        self.assertEqual(self._delivery_events()[0].status, "completed")

    def test_incomplete_mixed_and_missing_skladbot_never_send(self):
        for options in (
            {"scanned_blocks": 0},
            {"payment_type": "Терминал"},
            {"skladbot_request_number": ""},
        ):
            with self.subTest(options=options):
                self._seed_delivery(**options)
                with mock.patch.dict(os.environ, self.client_env, clear=False):
                    self.processor.process_pending_transfer_kiz_deliveries()
                self.assertEqual(self.sent, [])
                self.assertEqual(self._delivery_events()[-1].status, "blocked")

    def test_same_filename_is_isolated_by_exact_source_key(self):
        first = self._seed_delivery(source_file="same.xlsx")
        second = self._seed_delivery(source_file="same.xlsx")
        with mock.patch.dict(os.environ, self.client_env, clear=False):
            self.processor.process_pending_transfer_kiz_deliveries()

        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(first, second)
        self.assertEqual({event.payload["source_key"] for event in self._delivery_events()}, {first, second})

    def test_pre_send_build_failure_is_retryable_before_start(self):
        self._seed_delivery()
        claimed = self.processor._claim(TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE)
        with mock.patch(
            "backend.app.telegram_transfer_kiz_processor.build_kiz_source_file_report_xlsx",
            side_effect=RuntimeError("synthetic build failure"),
        ), mock.patch.dict(os.environ, self.client_env, clear=False):
            self.processor._deliver(*claimed)

        event = self._delivery_events()[0]
        self.assertEqual(event.status, "failed")
        self.assertFalse(event.payload.get("delivery_started"))
        self.assertEqual(self.sent, [])

    def test_ambiguous_send_blocks_without_retry_and_queues_one_admin_alert(self):
        self._seed_delivery()
        self.processor.send_document = mock.Mock(side_effect=RuntimeError("synthetic send failure"))
        with mock.patch.dict(os.environ, self.client_env, clear=False):
            self.processor.process_pending_transfer_kiz_deliveries()
            self.processor.process_pending_transfer_kiz_deliveries()

        delivery = self._delivery_events()[0]
        self.assertEqual(delivery.status, "blocked")
        self.assertEqual(self.processor.send_document.call_count, 1)
        alerts = self._notification_events()
        self.assertEqual(len(alerts), 1)
        self.assertNotIn("import:", alerts[0].payload["text"])

    def test_stale_started_delivery_is_blocked_without_send(self):
        self._seed_delivery(status="processing", delivery_started=True, stale=True)
        with mock.patch.dict(os.environ, self.client_env, clear=False):
            self.processor.recover_stale_transfer_kiz_events()

        self.assertEqual(self._delivery_events()[0].status, "blocked")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self._notification_events()), 1)

    def test_concurrent_sqlite_claims_do_not_claim_the_same_event(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite+pysqlite:///{directory}/transfer-kiz-claim.db",
                connect_args={"check_same_thread": False, "timeout": 5},
            )
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            event_id = uuid.uuid4()
            with session_factory() as db:
                db.add(PendingEvent(
                    id=event_id,
                    event_type=TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
                    idempotency_key=f"transfer-kiz:delivery:claim:{event_id}",
                    status="pending",
                    available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                ))
                db.commit()

            start = threading.Barrier(3)
            claimed, errors = [], []

            def claim_once():
                try:
                    processor = TelegramTransferKizProcessor(
                        ports=TelegramProcessorPorts(session_factory=session_factory, token="synthetic")
                    )
                    start.wait()
                    claimed.append(processor._claim(TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE))
                except Exception as exc:  # pragma: no cover - assertion below records thread failures.
                    errors.append(exc)

            threads = [threading.Thread(target=claim_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            successful_claims = [claim for claim in claimed if claim is not None]
            self.assertEqual(len(successful_claims), 1)
            self.assertEqual(successful_claims[0][0], event_id)
            with session_factory() as db:
                event = db.get(PendingEvent, event_id)
                self.assertEqual(event.status, "processing")
                self.assertEqual(event.attempts, 1)
                self.assertTrue(event.lease_owner)
                self.assertIsNotNone(event.lease_expires_at)
            Base.metadata.drop_all(engine)
            engine.dispose()

    def _seed_delivery(
        self,
        *,
        source_file="orders.xlsx",
        payment_type="Перечисление",
        skladbot_request_number="SB-1",
        scanned_blocks=1,
        status="pending",
        delivery_started=False,
        stale=False,
    ):
        import_id, order_id, item_id, scan_id, event_id = (uuid.uuid4() for _ in range(5))
        source_key = transfer_kiz_source_key(str(import_id), source_file)
        with self.SessionLocal() as db:
            db.add_all([
                ImportJob(
                    id=import_id, source="telegram", status="completed", rows_total=1, rows_imported=1,
                    raw_payload={"source_rows_count": 1, "skipped_rows_count": 0, "invalid_rows": 0},
                ),
                Order(
                    id=order_id, source="telegram", payment_type=payment_type, client="Synthetic",
                    address="Synthetic", status="not_completed",
                    raw_payload={"skladbot_request_number": skladbot_request_number},
                ),
                OrderItem(
                    id=item_id, order_id=order_id, product="Synthetic", quantity_pieces=10,
                    quantity_blocks=2 if not scanned_blocks else 1, scanned_blocks=scanned_blocks, requires_kiz=True,
                    status="completed" if scanned_blocks else "not_completed",
                    raw_payload={"backend_import_id": str(import_id), "source_file": source_file},
                ),
                ScanCode(id=scan_id, order_item_id=item_id, code=f"01{scan_id.hex}", source="desktop"),
                PendingEvent(
                    id=event_id, event_type=TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
                    action="deliver_transfer_kiz", aggregate_type="import_file", aggregate_id=source_key,
                    idempotency_key=f"transfer-kiz:delivery:test:{event_id}", status=status,
                    payload={"source_key": source_key, "delivery_started": delivery_started},
                    updated_at=(datetime.now(timezone.utc) - timedelta(minutes=20)) if stale else None,
                ),
            ])
            db.commit()
        return source_key

    def _delivery_events(self):
        with self.SessionLocal() as db:
            return db.execute(
                select(PendingEvent).where(PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE)
            ).scalars().all()

    def _notification_events(self):
        with self.SessionLocal() as db:
            return db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()


if __name__ == "__main__":
    unittest.main()
