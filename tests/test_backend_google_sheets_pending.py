import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_pending import (
    GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
    acquire_google_sheets_export_lock,
    load_skladbot_export_orders,
    process_pending_google_sheets_exports,
    queue_google_sheets_export,
    release_google_sheets_export_lock,
)
from backend.app.models import Base, Order, OrderItem, PendingEvent


class GoogleSheetsPendingLockTests(unittest.TestCase):
    def test_payload_based_google_export_uses_deterministic_idempotency_key(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            records = [{"ID заказа": "order-1", "Клиент": "Client"}]
            with SessionLocal() as db:
                first = queue_google_sheets_export(
                    db,
                    "google_sheets_import_export",
                    "import",
                    "import-1",
                    result={"status": "queued"},
                    payload={"records": records},
                )
                second = queue_google_sheets_export(
                    db,
                    "google_sheets_import_export",
                    "import",
                    "import-1",
                    result={"status": "queued"},
                    payload={"records": records},
                )
                db.commit()
                events = db.execute(select(PendingEvent)).scalars().all()

            self.assertEqual(str(first.id), str(second.id))
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0].idempotency_key.startswith("google_sheets:google_sheets_import_export:import:import-1:"))
            self.assertEqual(events[0].payload["idempotency_key"], events[0].idempotency_key)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_postgres_lock_does_not_use_session_level_advisory_lock(self):
        db = mock.Mock()
        db.bind.dialect.name = "postgresql"

        self.assertTrue(acquire_google_sheets_export_lock(db))
        release_google_sheets_export_lock(db)

        db.execute.assert_not_called()

    def test_rate_limit_keeps_event_pending_and_stops_batch(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "google_sheets_skladbot_export", "entity_id": "active_orders"},
                ))
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "google_sheets_import_export", "entity_id": "import-1"},
                ))
                db.commit()

                with mock.patch(
                    "backend.app.google_sheets_pending.run_google_sheets_export_event",
                    side_effect=RuntimeError("APIError: [429]: Quota exceeded"),
                ) as export_mock, mock.patch("backend.app.google_sheets_pending.logger.exception"), mock.patch(
                    "backend.app.google_sheets_pending.logger.warning"
                ):
                    result = process_pending_google_sheets_exports(db, limit=50)

                events = db.execute(
                    select(PendingEvent).order_by(PendingEvent.created_at, PendingEvent.id)
                ).scalars().all()

            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["remaining"], 2)
            self.assertEqual(export_mock.call_count, 1)
            self.assertEqual([event.status for event in events], ["pending", "pending"])
            self.assertEqual(events[0].attempts, 1)
            self.assertIn("429", events[0].last_error)
            self.assertTrue((events[0].payload or {}).get("next_attempt_at"))
            self.assertEqual(events[1].attempts, 0)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_bad_event_does_not_block_newer_valid_event(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "unknown_google_action", "entity_id": "bad-1"},
                ))
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={
                        "action": "google_sheets_import_export",
                        "entity_id": "import-1",
                        "records": [{"ID заказа": "order-1"}],
                    },
                ))
                db.commit()

                with mock.patch(
                    "backend.app.google_sheets_pending.append_import_records_to_google_sheets",
                    return_value={"status": "completed", "appended": 1},
                ) as append_mock:
                    result = process_pending_google_sheets_exports(db, limit=50)

                events = db.execute(select(PendingEvent)).scalars().all()
                events_by_entity_id = {
                    str((event.payload or {}).get("entity_id") or ""): event
                    for event in events
                }

            self.assertEqual(result["status"], "completed_with_errors")
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["synced"], 1)
            self.assertEqual(events_by_entity_id["bad-1"].status, "failed")
            self.assertEqual(events_by_entity_id["import-1"].status, "completed")
            append_mock.assert_called_once()
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_archive_events_are_batched_before_single_event_processing(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                order_one = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Client One",
                    address="Tashkent",
                    status="completed",
                    raw_payload={},
                )
                order_one.items.append(OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=1,
                    status="completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                ))
                order_two = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Client Two",
                    address="Tashkent",
                    status="completed",
                    raw_payload={},
                )
                order_two.items.append(OrderItem(
                    product="Chapman RED OP 20",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=1,
                    status="completed",
                    raw_payload={"source_import_id": "import-2", "source_order_id": "order-2"},
                ))
                db.add_all([order_one, order_two])
                db.flush()
                order_one_id = str(order_one.id)
                order_two_id = str(order_two.id)
                db.add_all([
                    PendingEvent(
                        event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                        status="pending",
                        payload={"action": "google_sheets_archive_export", "entity_id": order_one_id},
                    ),
                    PendingEvent(
                        event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                        status="pending",
                        payload={"action": "google_sheets_archive_export", "entity_id": order_two_id},
                    ),
                ])
                db.commit()

                def fake_archive(orders):
                    return {
                        "status": "completed",
                        "updated": len(list(orders)),
                        "orders": {
                            order_one_id: {"status": "completed", "updated": 1},
                            order_two_id: {"status": "completed", "updated": 1},
                        },
                    }

                with mock.patch(
                    "backend.app.google_sheets_pending.archive_backend_orders_to_google_sheets",
                    side_effect=fake_archive,
                ) as archive_mock, mock.patch(
                    "backend.app.google_sheets_pending.run_google_sheets_export_event"
                ) as single_export_mock:
                    result = process_pending_google_sheets_exports(db, limit=50)

                events = db.execute(
                    select(PendingEvent).order_by(PendingEvent.created_at, PendingEvent.id)
                ).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["synced"], 2)
            self.assertEqual(archive_mock.call_count, 1)
            self.assertEqual(single_export_mock.call_count, 0)
            self.assertEqual([event.status for event in events], ["completed", "completed"])
            self.assertEqual([event.attempts for event in events], [1, 1])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_terminal_scan_event_missing_google_row_is_skipped(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                order = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Client",
                    address="Tashkent",
                    status="completed",
                    raw_payload={},
                )
                item = OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=1,
                    status="completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                )
                order.items.append(item)
                db.add(order)
                db.flush()
                item_id = str(item.id)
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "google_sheets_scan_export", "entity_id": item_id},
                ))
                db.commit()

                with mock.patch(
                    "backend.app.google_sheets_pending.sync_backend_order_items_to_google_sheets",
                    return_value={"status": "missing", "error": "order item rows not found"},
                ):
                    result = process_pending_google_sheets_exports(db, limit=50)

                event = db.execute(select(PendingEvent)).scalar_one()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["synced"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(event.status, "completed")
            self.assertEqual((event.payload or {}).get("last_result", {}).get("status"), "skipped")
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_skladbot_export_can_include_completed_order_for_archive_backfill(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                order = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Client",
                    address="Tashkent",
                    status="completed",
                    raw_payload={"skladbot_request_number": "WH-R-1"},
                )
                order.items.append(OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=1,
                    status="completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                ))
                db.add(order)
                db.flush()
                order_id = str(order.id)

                without_inactive = load_skladbot_export_orders(db, {"order_ids": [order_id]})
                with_inactive = load_skladbot_export_orders(
                    db,
                    {"order_ids": [order_id], "include_inactive": True},
                )

            self.assertEqual(without_inactive, [])
            self.assertEqual([str(order.id) for order in with_inactive], [order_id])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_skladbot_export_event_passes_archive_mode_to_exporter(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                order = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Client",
                    address="Tashkent",
                    status="completed",
                    raw_payload={"skladbot_request_number": "WH-R-1"},
                )
                order.items.append(OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=1,
                    status="completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                ))
                db.add(order)
                db.flush()
                order_id = str(order.id)
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={
                        "action": "google_sheets_skladbot_export",
                        "entity_id": "skladbot",
                        "order_ids": [order_id],
                        "include_inactive": True,
                        "include_archive": True,
                    },
                ))
                db.commit()

                captured_order_ids = []
                captured_include_archive = []

                def fake_skladbot_export(orders, include_archive=False):
                    captured_order_ids.extend(str(row.id) for row in orders)
                    captured_include_archive.append(include_archive)
                    return {"status": "completed", "updated": 1}

                with mock.patch(
                    "backend.app.google_sheets_pending.sync_backend_orders_skladbot_to_google_sheets",
                    side_effect=fake_skladbot_export,
                ) as export_mock:
                    result = process_pending_google_sheets_exports(db, limit=50)

                event = db.execute(select(PendingEvent)).scalar_one()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["synced"], 1)
            self.assertEqual(event.status, "completed")
            export_mock.assert_called_once()
            self.assertEqual(captured_include_archive, [True])
            self.assertEqual(captured_order_ids, [order_id])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_stale_processing_event_is_reset_and_processed(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="processing",
                    attempts=3,
                    payload={"action": "google_sheets_import_export", "entity_id": "import-1", "records": [{"x": 1}]},
                    last_error="old processing",
                    updated_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                ))
                db.commit()

                with mock.patch(
                    "backend.app.google_sheets_pending.run_google_sheets_export_event",
                    return_value={"status": "skipped"},
                ) as export_mock:
                    result = process_pending_google_sheets_exports(db, limit=50)

                event = db.execute(select(PendingEvent)).scalar_one()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["synced"], 1)
            self.assertEqual(event.status, "completed")
            self.assertEqual(event.attempts, 4)
            self.assertEqual(export_mock.call_count, 1)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
