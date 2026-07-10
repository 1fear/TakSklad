import os
import unittest
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.admin_service import build_admin_table, build_totals, filter_admin_rows
from backend.app.event_queue_service import list_event_queue_diagnostics
from backend.app.models import ImportJob, Order, OrderItem, PendingEvent, ScanCode
from backend.app.orders_service import list_active_orders
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

    def test_event_summary_is_full_history_but_rows_are_exactly_bounded(self):
        with self.SessionLocal() as db:
            diagnostics = list_event_queue_diagnostics(db, limit=2)

        self.assertEqual(diagnostics["summary"]["total"], 3)
        self.assertEqual(len(diagnostics["recent_events"]), 2)
        self.assertEqual(len(diagnostics["stale_processing"]), 0)


if __name__ == "__main__":
    unittest.main()
