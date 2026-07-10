import os
import unittest
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.admin_service import build_admin_table, build_totals, filter_admin_rows
from backend.app.event_queue_service import list_event_queue_diagnostics
from backend.app.models import ImportJob, Order, OrderItem, PendingEvent, ScanCode
from backend.app.orders_service import list_active_orders, list_active_orders_page
from backend.app.reports_service import build_dashboard_day_summary, build_day_report
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresQueryParityTests(unittest.TestCase):
    database = "taksklad_query_parity"
    report_date = date(2026, 6, 1)

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
        self.seed_fixture()

    def seed_fixture(self):
        with self.SessionLocal() as db:
            imported = ImportJob(
                source="excel",
                status="completed",
                rows_total=3,
                rows_imported=3,
                created_at=datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc),
                raw_payload={"synthetic": True},
            )
            db.add(imported)
            db.flush()
            active = Order(
                source="excel",
                order_date=self.report_date,
                payment_type="Терминал",
                client="Synthetic Active",
                address="Synthetic Address",
                status="not_completed",
                raw_payload={"skladbot_request_number": "WH-R-SYNTHETIC"},
                created_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
            )
            active.items = [
                OrderItem(
                    product="Synthetic Product A",
                    quantity_pieces=20,
                    quantity_blocks=2,
                    scanned_blocks=1,
                    status="not_completed",
                    raw_payload={
                        "line_total": 200,
                        "backend_import_id": str(imported.id),
                        "source_file": "synthetic.xlsx",
                    },
                ),
                OrderItem(
                    product="Synthetic Product B",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={"line_total": 100, "backend_import_id": str(imported.id)},
                ),
            ]
            completed = Order(
                source="excel",
                order_date=self.report_date,
                payment_type="Перечисление",
                client="Synthetic Completed",
                address="Synthetic Address",
                status="completed",
                raw_payload={},
                created_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
            )
            completed.items = [OrderItem(
                product="Synthetic Product C",
                quantity_pieces=10,
                quantity_blocks=1,
                scanned_blocks=1,
                status="completed",
                raw_payload={"line_total": 150, "backend_import_id": str(imported.id)},
            )]
            historical = Order(
                source="excel",
                order_date=date(2025, 1, 1),
                payment_type="Терминал",
                client="Historical Noise",
                address="Historical Address",
                status="not_completed",
                raw_payload={},
                created_at=datetime(2025, 1, 1, 8, 0, tzinfo=timezone.utc),
            )
            historical.items = [OrderItem(
                product="Historical Product",
                quantity_pieces=10,
                quantity_blocks=1,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={"line_total": 50},
            )]
            db.add_all([active, completed, historical])
            db.flush()
            db.add(ScanCode(
                order_item_id=active.items[0].id,
                code="SYNTHETIC-KIZ-1",
                scanned_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
                raw_payload={"block_quantity": 1},
            ))
            db.add_all([
                PendingEvent(
                    event_type="google_sheets_export",
                    status="pending",
                    payload={"entity_id": str(active.id), "synthetic": True},
                ),
                PendingEvent(
                    event_type="synthetic_query_parity",
                    status="failed",
                    payload={"sequence": 1},
                ),
                PendingEvent(
                    event_type="synthetic_query_parity",
                    status="completed",
                    payload={"sequence": 2},
                ),
            ])
            db.commit()

    def test_admin_sql_candidate_matches_characterized_python_filter_and_totals(self):
        with self.SessionLocal() as db:
            unfiltered = build_admin_table(db, limit=100, activity_limit=0)
            expected_rows = filter_admin_rows(
                unfiltered.rows,
                status_bucket="active",
                shipment_date=self.report_date.isoformat(),
                search="synthetic active",
                scan_state="",
                skladbot_filter="found",
                google_status="pending",
            )
            pending_events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalars().all()
            expected_totals = build_totals(expected_rows, pending_events)
            actual = build_admin_table(
                db,
                limit=100,
                activity_limit=0,
                status_bucket="active",
                shipment_date=self.report_date.isoformat(),
                search="synthetic active",
                skladbot_filter="found",
                google_status="pending",
            )

        self.assertEqual([row.item_id for row in actual.rows], [row.item_id for row in expected_rows])
        self.assertEqual(actual.totals.model_dump(), expected_totals.model_dump())
        self.assertEqual(actual.total_rows, len(expected_rows))

    def test_admin_sql_filter_matrix_preserves_python_contract_and_pending_counts(self):
        with self.SessionLocal() as db:
            active = db.execute(select(Order).where(Order.client == "Synthetic Active")).scalar_one()
            active.raw_payload = {**active.raw_payload, "skladbot_status": "pending"}
            active.items[0].product = "Synthetic 100% Product A"
            completed = db.execute(select(Order).where(Order.client == "Synthetic Completed")).scalar_one()
            completed.items[0].raw_payload = {
                **completed.items[0].raw_payload,
                "google_sheet_synced_at": "2026-06-01T12:00:00+00:00",
            }
            removed = Order(
                source="synthetic",
                order_date=date(2024, 1, 1),
                payment_type="Терминал",
                client="Synthetic Removed",
                address="Removed Address",
                status="not_completed",
                raw_payload={},
            )
            removed.items = [OrderItem(
                product="Removed Product",
                quantity_pieces=0,
                quantity_blocks=0,
                scanned_blocks=0,
                status="removed_from_google_sheet",
                raw_payload={},
            )]
            returned = Order(
                source="synthetic",
                order_date=date(2024, 1, 2),
                payment_type="Перечисление",
                client="Synthetic Returned",
                address="Returned Address",
                status="returned",
                raw_payload={"return_status": "returned"},
            )
            returned.items = [OrderItem(
                product="Returned Product",
                quantity_pieces=10,
                quantity_blocks=1,
                scanned_blocks=1,
                status="returned",
                raw_payload={"line_total": 75},
            )]
            db.add_all([removed, returned])
            db.flush()
            db.add_all([
                PendingEvent(
                    event_type="google_sheets_export",
                    status="pending",
                    payload={
                        "action": "google_sheets_bulk_export",
                        "entity_id": "synthetic-bulk",
                        "order_ids": [str(active.id)],
                    },
                ),
                PendingEvent(
                    event_type="google_sheets_export",
                    status="pending",
                    payload={
                        "action": "google_sheets_skladbot_export",
                        "entity_id": str(active.items[0].id),
                    },
                ),
            ])
            db.commit()

            characterized = build_admin_table(db, limit=100, activity_limit=0)
            pending_events = [
                pending for pending in db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
                ).scalars().all()
                if (pending.payload or {}).get("action") != "google_sheets_skladbot_export"
            ]
            cases = (
                {"status_bucket": "active"},
                {"status_bucket": "archive"},
                {"status_bucket": "returned"},
                {"status_bucket": "removed_from_google"},
                {"shipment_date": self.report_date.isoformat()},
                {"search": "100% product"},
                {"search": "терминал"},
                {"scan_state": "no_plan"},
                {"scan_state": "not_started"},
                {"scan_state": "in_progress"},
                {"scan_state": "completed"},
                {"skladbot_filter": "found"},
                {"skladbot_filter": "missing"},
                {"skladbot_filter": "problem"},
                {"google_status": "pending"},
                {"google_status": "synced"},
                {"google_status": "removed_from_google"},
                {"google_status": "unknown"},
            )
            for filters in cases:
                with self.subTest(filters=filters):
                    expected_rows = filter_admin_rows(characterized.rows, **filters)
                    expected_totals = build_totals(expected_rows, pending_events)
                    actual = build_admin_table(db, limit=100, activity_limit=0, **filters)
                    self.assertEqual(
                        [row.item_id for row in actual.rows],
                        [row.item_id for row in expected_rows],
                    )
                    self.assertEqual(actual.totals.model_dump(), expected_totals.model_dump())
                    self.assertEqual(actual.total_rows, len(expected_rows))

            active_rows = [row for row in characterized.rows if row.client == "Synthetic Active"]
            self.assertEqual({row.pending_google_exports for row in active_rows}, {2})
            self.assertEqual(characterized.totals.pending_google_exports, 2)

    def test_admin_first_page_query_count_is_constant_when_history_grows(self):
        def measure():
            counter = {"value": 0}

            def count_query(*_args):
                counter["value"] += 1

            event.listen(self.engine, "before_cursor_execute", count_query)
            try:
                with self.SessionLocal() as db:
                    result = build_admin_table(db, limit=2, activity_limit=0)
                    self.assertEqual(result.row_count, 2)
            finally:
                event.remove(self.engine, "before_cursor_execute", count_query)
            return counter["value"]

        before = measure()
        with self.SessionLocal() as db:
            for index in range(100):
                order = Order(
                    source="synthetic_history",
                    order_date=date(2020, 1, 1),
                    payment_type="Терминал",
                    client=f"History {index:03d}",
                    address="Synthetic History",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(
                    product="History Product",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={"line_total": 1},
                )]
                db.add(order)
            db.commit()
        after = measure()

        self.assertEqual(before, after)
        self.assertLessEqual(after, 3)

    def test_active_orders_and_day_reports_preserve_business_totals(self):
        with self.SessionLocal() as db:
            active = list_active_orders(db)
            day = build_day_report(db, self.report_date.isoformat())
            dashboard = build_dashboard_day_summary(db, self.report_date.isoformat())

        self.assertEqual([order.client for order in active], ["Historical Noise", "Synthetic Active"])
        self.assertEqual(day.totals.orders, 2)
        self.assertEqual(day.totals.items, 3)
        self.assertEqual(day.totals.planned_blocks, 4)
        self.assertEqual(day.totals.scanned_blocks, 2)
        self.assertEqual(day.totals.scanned_today, 1)
        self.assertEqual(day.totals.total_price, 450)
        self.assertEqual(dashboard.totals.orders, 2)
        self.assertEqual(dashboard.totals.items, 3)

    def test_active_first_page_query_count_is_constant_at_ten_x_history(self):
        def measure():
            counter = {"value": 0}

            def count_query(*_args):
                counter["value"] += 1

            event.listen(self.engine, "before_cursor_execute", count_query)
            try:
                with self.SessionLocal() as db:
                    rows, _cursor, _limit = list_active_orders_page(db, limit=2)
                    self.assertEqual(len(rows), 2)
            finally:
                event.remove(self.engine, "before_cursor_execute", count_query)
            return counter["value"]

        before = measure()
        with self.SessionLocal() as db:
            for index in range(30):
                order = Order(
                    source="synthetic_active_history",
                    order_date=date(2024, 1, 1),
                    payment_type="Терминал",
                    client=f"Active History {index:03d}",
                    address="Synthetic History",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(
                    product="History Product",
                    quantity_pieces=10,
                    quantity_blocks=1,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={},
                )]
                db.add(order)
            db.commit()
        after = measure()

        self.assertEqual(before, after)
        self.assertLessEqual(after, 4)

        with self.SessionLocal() as db:
            first, cursor, _limit = list_active_orders_page(db, limit=1)
            second, _next, _limit = list_active_orders_page(db, limit=1, cursor=cursor)
            expected = list_active_orders(db, limit=2)
        self.assertTrue(cursor)
        self.assertEqual(
            [row.id for row in first + second],
            [row.id for row in expected],
        )
        self.assertEqual(len({row.id for row in first + second}), 2)

    def test_day_report_uses_sql_scoped_candidates_with_timezone_fallback_and_aggregate_boxes(self):
        with self.SessionLocal() as db:
            cross_date = Order(
                source="excel",
                order_date=date(2026, 5, 31),
                payment_type="Терминал",
                client="Cross-date scan",
                address="Synthetic Address",
                status="not_completed",
                raw_payload={},
                created_at=datetime(2026, 5, 31, 8, 0, tzinfo=timezone.utc),
            )
            cross_date.items = [
                OrderItem(
                    product="Aggregate product",
                    quantity_pieces=1200,
                    quantity_blocks=60,
                    scanned_blocks=51,
                    status="not_completed",
                    raw_payload={"line_total": "500"},
                ),
                OrderItem(
                    product="Unscanned sibling",
                    quantity_pieces=40,
                    quantity_blocks=2,
                    scanned_blocks=0,
                    status="completed",
                    raw_payload={"line_total": 200},
                ),
            ]
            db.add(cross_date)
            db.flush()
            db.add_all([
                ScanCode(
                    order_item_id=cross_date.items[0].id,
                    code="0104006396053985SYNTHETIC-BOX",
                    scanned_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
                    raw_payload={"scanned_at": "2026-05-31T20:30:00+00:00"},
                ),
                ScanCode(
                    order_item_id=cross_date.items[0].id,
                    code="SYNTHETIC-UNIT",
                    scanned_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
                    raw_payload={"scanned_at": "invalid", "block_quantity": 1},
                ),
            ])
            db.commit()

        query_count = {"value": 0}

        def count_query(*_args):
            query_count["value"] += 1

        event.listen(self.engine, "before_cursor_execute", count_query)
        try:
            with self.SessionLocal() as db:
                report = build_day_report(db, self.report_date.isoformat())
        finally:
            event.remove(self.engine, "before_cursor_execute", count_query)

        self.assertEqual(query_count["value"], 1)
        self.assertEqual(report.totals.orders, 3)
        self.assertEqual(report.totals.completed_orders, 1)
        self.assertEqual(report.totals.active_orders, 2)
        self.assertEqual(report.totals.items, 5)
        self.assertEqual(report.totals.completed_items, 2)
        self.assertEqual(report.totals.planned_blocks, 66)
        self.assertEqual(report.totals.scanned_blocks, 53)
        self.assertEqual(report.totals.scanned_today, 52)
        self.assertEqual(report.totals.remaining_blocks, 13)
        self.assertEqual(report.totals.scan_codes, 3)
        self.assertEqual(report.totals.total_price, 1150)
        cross_date_row = next(row for row in report.orders if row.client == "Cross-date scan")
        self.assertEqual(cross_date_row.items, 2)
        self.assertEqual(cross_date_row.scanned_today, 51)

    def test_dashboard_uses_loaded_item_sql_aggregates_and_scoped_return_candidates(self):
        target_loaded_at = datetime(2026, 5, 31, 20, 30, tzinfo=timezone.utc)
        old_loaded_at = datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc)
        with self.SessionLocal() as db:
            target_import = ImportJob(
                source="excel",
                status="completed",
                rows_total=1,
                rows_imported=1,
                created_at=target_loaded_at,
                raw_payload={"synthetic": True},
            )
            db.add(target_import)
            db.flush()
            imported_active = Order(
                source="excel",
                order_date=date(2026, 6, 2),
                payment_type="Терминал",
                client="Loaded by import",
                address="Synthetic Address",
                status="not_completed",
                raw_payload={},
            )
            imported_active.items = [OrderItem(
                product="Imported item",
                quantity_pieces=80,
                quantity_blocks=4,
                scanned_blocks=1,
                status="not_completed",
                created_at=old_loaded_at,
                raw_payload={"backend_import_id": str(target_import.id), "line_total": 400},
            )]
            fallback_completed = Order(
                source="excel",
                order_date=date(2026, 6, 2),
                payment_type="Перечисление",
                client="Loaded by item fallback",
                address="Synthetic Address",
                status="completed",
                raw_payload={},
            )
            fallback_completed.items = [
                OrderItem(
                    product="Fallback item",
                    quantity_pieces=40,
                    quantity_blocks=2,
                    scanned_blocks=2,
                    status="completed",
                    created_at=datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc),
                    raw_payload={"backend_import_id": "invalid-import-id", "line_total": "200"},
                ),
                OrderItem(
                    product="Removed item",
                    quantity_pieces=1980,
                    quantity_blocks=99,
                    scanned_blocks=99,
                    status="removed_from_google_sheet",
                    created_at=target_loaded_at,
                    raw_payload={"line_total": 9900},
                ),
            ]
            archived = Order(
                source="excel",
                order_date=self.report_date,
                payment_type="Терминал",
                client="Archived loaded item",
                address="Synthetic Address",
                status="archived_no_kiz",
                raw_payload={},
            )
            archived.items = [OrderItem(
                product="Archived",
                quantity_pieces=20,
                quantity_blocks=1,
                scanned_blocks=0,
                status="archived_no_kiz",
                created_at=target_loaded_at,
                raw_payload={"line_total": 100},
            )]
            returned = Order(
                source="excel",
                order_date=date(2026, 5, 30),
                payment_type="Терминал",
                client="Returned fallback",
                address="Synthetic Address",
                status="not_completed",
                updated_at=target_loaded_at,
                raw_payload={"return_status": "RETURN", "returned_at": "invalid"},
            )
            returned.items = [OrderItem(
                product="Returned",
                quantity_pieces=20,
                quantity_blocks=1,
                scanned_blocks=1,
                status="completed",
                created_at=target_loaded_at,
                raw_payload={"line_total": 100},
            )]
            db.add_all([imported_active, fallback_completed, archived, returned])
            db.commit()

        query_count = {"value": 0}

        def count_query(*_args):
            query_count["value"] += 1

        event.listen(self.engine, "before_cursor_execute", count_query)
        try:
            with self.SessionLocal() as db:
                dashboard = build_dashboard_day_summary(db, self.report_date.isoformat())
        finally:
            event.remove(self.engine, "before_cursor_execute", count_query)

        self.assertEqual(query_count["value"], 2)
        self.assertEqual(dashboard.totals.orders, 4)
        self.assertEqual(dashboard.totals.completed_orders, 2)
        self.assertEqual(dashboard.totals.active_orders, 2)
        self.assertEqual(dashboard.totals.returned_orders, 1)
        self.assertEqual(dashboard.totals.items, 5)
        self.assertEqual(dashboard.totals.completed_items, 2)
        self.assertEqual(dashboard.totals.planned_blocks, 10)
        self.assertEqual(dashboard.totals.scanned_blocks, 5)
        self.assertEqual(dashboard.totals.scanned_today, 1)
        self.assertEqual(dashboard.totals.remaining_blocks, 5)
        self.assertEqual(dashboard.totals.scan_codes, 1)
        self.assertEqual(dashboard.totals.total_price, 1050)

    def test_event_summary_is_full_history_but_rows_are_exactly_bounded(self):
        with self.SessionLocal() as db:
            diagnostics = list_event_queue_diagnostics(db, limit=2)

        self.assertEqual(diagnostics["summary"]["total"], 3)
        self.assertEqual(len(diagnostics["recent_events"]), 2)
        self.assertEqual(len(diagnostics["stale_processing"]), 0)


if __name__ == "__main__":
    unittest.main()
