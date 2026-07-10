"""PostgreSQL contract tests for stable admin-table cursors and action capabilities."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import date, datetime, timezone

from fastapi import Response
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from backend.app.models import Order, OrderItem, PendingEvent, ScanCode
from backend.app.pagination import CursorError
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))
ACTION_KEYS = {
    "resync",
    "archive",
    "completeWithoutKiz",
    "cancel",
    "deleteActive",
    "resetRescan",
    "restore",
    "resyncSkladBot",
}


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresCursorCapabilityTests(unittest.TestCase):
    database = "taksklad_cursor_capabilities"

    @classmethod
    def setUpClass(cls):
        cls.url = create_database(cls.database)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE TABLE audit_log, incidents, pending_events, scan_codes, order_items, "
                "orders, import_files, imports CASCADE"
            ))

    def seed_order(
        self,
        db,
        *,
        identity: int,
        shipment_date: date,
        client: str,
        created_at: datetime,
        quantities: tuple[int, ...] = (1,),
    ) -> Order:
        order = Order(
            id=uuid.UUID(int=identity),
            source="phase19-synthetic",
            order_date=shipment_date,
            payment_type="Synthetic",
            client=client,
            address="Synthetic Address",
            status="not_completed",
            raw_payload={},
            created_at=created_at,
            updated_at=created_at,
        )
        order.items = [
            OrderItem(
                id=uuid.UUID(int=identity * 100 + index),
                product=f"Synthetic Product {identity}-{index}",
                quantity_pieces=quantity * 10,
                quantity_blocks=quantity,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={},
                created_at=created_at,
                updated_at=created_at,
            )
            for index, quantity in enumerate(quantities, start=1)
        ]
        db.add(order)
        db.flush()
        return order

    @staticmethod
    def load_page(
        db,
        *,
        limit: int,
        cursor: str = "",
        search: str = "",
        status_bucket: str = "active",
        google_status: str = "",
    ):
        # Import after the disposable database is migrated; the endpoint is invoked
        # directly with the isolated session and never touches configured runtime data.
        from backend.app.main import admin_table

        return admin_table(
            response=Response(),
            limit=limit,
            offset=0,
            cursor=cursor,
            activity_limit=0,
            status_bucket=status_bucket,
            shipment_date="",
            search=search,
            scan_state="",
            skladbot_filter="",
            google_status=google_status,
            google_sheet_status="",
            db=db,
        )

    def test_snapshot_keyset_cursor_has_no_duplicate_or_gap_after_boundary_insert(self):
        seeded_at = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
        with self.SessionLocal() as db:
            originals = [
                self.seed_order(
                    db,
                    identity=identity,
                    shipment_date=date(2026, 6, day),
                    client=f"Cursor Original {identity}",
                    created_at=seeded_at,
                )
                for identity, day in ((1, 1), (2, 2), (4, 4), (5, 5))
            ]
            db.commit()

            first = self.load_page(db, limit=2, search="Cursor")
            first_ids = [row.order_id for row in first.rows]
            self.assertEqual(first_ids, [str(originals[0].id), str(originals[1].id)])
            self.assertTrue(first.next_cursor)

            inserted = self.seed_order(
                db,
                identity=3,
                shipment_date=date(2026, 6, 3),
                client="Cursor Inserted After Page One",
                created_at=datetime.now(timezone.utc),
            )
            db.commit()

            second = self.load_page(db, limit=2, cursor=first.next_cursor, search="Cursor")
            second_ids = [row.order_id for row in second.rows]

        original_ids = [str(uuid.UUID(int=identity)) for identity in (1, 2, 4, 5)]
        combined_ids = first_ids + second_ids
        self.assertEqual(second_ids, original_ids[2:])
        self.assertEqual(combined_ids, original_ids)
        self.assertEqual(len(combined_ids), len(set(combined_ids)))
        self.assertNotIn(str(uuid.UUID(int=3)), combined_ids)
        self.assertFalse(second.next_cursor)

    def test_order_capability_uses_complete_order_not_partial_item_page(self):
        with self.SessionLocal() as db:
            order = self.seed_order(
                db,
                identity=20,
                shipment_date=date(2026, 6, 20),
                client="Capability Complete Active",
                created_at=datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc),
                quantities=(2, 3),
            )
            db.commit()

            page = self.load_page(db, limit=1, search="Capability Complete Active")

        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0].quantity_blocks, 2)
        order_id = str(uuid.UUID(int=20))
        capability = page.order_capabilities[order_id]
        self.assertEqual(capability.order_id, order_id)
        self.assertEqual(capability.planned_blocks, 5)
        self.assertEqual(capability.scanned_blocks, 0)
        self.assertEqual(capability.scan_codes_count, 0)
        self.assertEqual(capability.pending_google_exports, 0)
        self.assertEqual(set(capability.allowed), ACTION_KEYS)
        self.assertEqual(set(capability.disabled_reasons), ACTION_KEYS)
        self.assertTrue(capability.allowed["completeWithoutKiz"])
        self.assertTrue(capability.allowed["archive"])
        self.assertTrue(capability.allowed["deleteActive"])
        self.assertFalse(capability.allowed["restore"])
        self.assertEqual(capability.disabled_reasons["restore"],
                         "Доступно только для отмененных заказов или архива без КИЗов")

    def test_cursor_crosses_dated_to_null_rows_and_rejects_filter_drift(self):
        created_at = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
        with self.SessionLocal() as db:
            dated = self.seed_order(
                db,
                identity=30,
                shipment_date=date(2026, 6, 1),
                client="Null Boundary Dated",
                created_at=created_at,
            )
            undated = self.seed_order(
                db,
                identity=31,
                shipment_date=None,
                client="Null Boundary Undated",
                created_at=created_at,
            )
            db.commit()
            dated_id = str(dated.id)
            undated_id = str(undated.id)

            first = self.load_page(db, limit=1, search="Null Boundary")
            second = self.load_page(db, limit=1, cursor=first.next_cursor, search="Null Boundary")
            with self.assertRaisesRegex(CursorError, "invalid_cursor"):
                self.load_page(db, limit=1, cursor=first.next_cursor, search="different filter")

        self.assertEqual([row.order_id for row in first.rows], [dated_id])
        self.assertEqual([row.order_id for row in second.rows], [undated_id])
        self.assertFalse(second.next_cursor)

    def test_capability_aggregates_hidden_scans_pending_and_keeps_query_budget(self):
        with self.SessionLocal() as db:
            order = self.seed_order(
                db,
                identity=40,
                shipment_date=date(2026, 6, 20),
                client="Capability Hidden State",
                created_at=datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc),
                quantities=(2, 3),
            )
            hidden_item = order.items[1]
            hidden_item.scanned_blocks = 1
            hidden_item.scan_codes.append(ScanCode(code="phase19-hidden-kiz", raw_payload={}))
            db.add(PendingEvent(
                event_type="google_sheets_export",
                status="pending",
                idempotency_key="phase19-hidden-pending",
                payload={"entity_id": str(hidden_item.id), "action": "google_sheets_archive_export"},
            ))
            db.commit()
            order_id = str(order.id)

            statements = []
            def capture(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            event.listen(self.engine, "before_cursor_execute", capture)
            try:
                page = self.load_page(db, limit=1, search="Capability Hidden State")
            finally:
                event.remove(self.engine, "before_cursor_execute", capture)

        capability = page.order_capabilities[order_id]
        self.assertEqual(page.rows[0].scanned_blocks, 0)
        self.assertEqual(capability.items_count, 2)
        self.assertEqual(capability.planned_blocks, 5)
        self.assertEqual(capability.scanned_blocks, 1)
        self.assertEqual(capability.scan_codes_count, 1)
        self.assertEqual(capability.pending_google_exports, 1)
        self.assertFalse(capability.allowed["archive"])
        self.assertFalse(capability.allowed["completeWithoutKiz"])
        self.assertLessEqual(len(statements), 3)

    def test_row_google_status_stays_item_scoped_while_capability_is_order_scoped(self):
        with self.SessionLocal() as db:
            order = self.seed_order(
                db,
                identity=45,
                shipment_date=date(2026, 6, 20),
                client="Capability Row Pending",
                created_at=datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc),
                quantities=(2, 3),
            )
            pending_item = order.items[1]
            db.add(PendingEvent(
                event_type="google_sheets_export",
                status="pending",
                idempotency_key="phase19-row-pending",
                payload={"entity_id": str(pending_item.id), "action": "google_sheets_archive_export"},
            ))
            db.commit()
            order_id = str(order.id)
            pending_item_id = str(pending_item.id)

            page = self.load_page(db, limit=2, search="Capability Row Pending")
            filtered = self.load_page(
                db,
                limit=2,
                search="Capability Row Pending",
                google_status="pending",
            )

        self.assertEqual([row.pending_google_exports for row in page.rows], [0, 1])
        self.assertEqual([row.google_sheet_status for row in page.rows], ["unknown", "pending"])
        self.assertEqual([row.item_id for row in filtered.rows], [pending_item_id])
        self.assertEqual(page.order_capabilities[order_id].pending_google_exports, 1)

    def test_capability_status_matrix_matches_backend_action_contract(self):
        with self.SessionLocal() as db:
            cancelled = self.seed_order(
                db,
                identity=50,
                shipment_date=date(2026, 6, 21),
                client="Capability Matrix Cancelled",
                created_at=datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),
            )
            cancelled.status = "cancelled"
            returned = self.seed_order(
                db,
                identity=51,
                shipment_date=date(2026, 6, 22),
                client="Capability Matrix Returned",
                created_at=datetime(2026, 6, 22, 6, 0, tzinfo=timezone.utc),
            )
            returned.status = "returned"
            db.commit()
            cancelled_id = str(cancelled.id)
            returned_id = str(returned.id)

            page = self.load_page(db, limit=10, search="Capability Matrix", status_bucket="")

        cancelled_capability = page.order_capabilities[cancelled_id]
        self.assertTrue(cancelled_capability.allowed["restore"])
        self.assertTrue(cancelled_capability.allowed["resetRescan"])
        self.assertTrue(cancelled_capability.allowed["resync"])
        self.assertTrue(cancelled_capability.allowed["resyncSkladBot"])
        self.assertFalse(cancelled_capability.allowed["archive"])
        self.assertFalse(cancelled_capability.allowed["completeWithoutKiz"])

        returned_capability = page.order_capabilities[returned_id]
        self.assertFalse(returned_capability.allowed["restore"])
        self.assertFalse(returned_capability.allowed["resetRescan"])
        self.assertTrue(returned_capability.allowed["resync"])
        self.assertTrue(returned_capability.allowed["resyncSkladBot"])


if __name__ == "__main__":
    unittest.main()
