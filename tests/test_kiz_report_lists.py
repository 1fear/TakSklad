import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.kiz_reports_service import (
    build_kiz_date_range_report_xlsx,
    build_kiz_date_report_xlsx,
    list_completed_kiz_dates,
    list_completed_kiz_source_files,
)
from backend.app.models import Base, ImportJob, Order, OrderItem, ScanCode


class KizReportListsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_lists_do_not_build_order_item_entities(self):
        # 13.10 списки файлов и дат для меню бота поднимали всю историю ORM-объектами
        # и считались 26-34 с: дольше таймаута бота (20 с) и лимита простоя транзакции
        for index in range(3):
            self._seed_file(f"file-{index}.xlsx", date(2026, 10, 10 + index), quantities=(1, 1))

        loaded = []

        def record_load(target, _context):
            loaded.append(target.id)

        sa_event.listen(OrderItem, "load", record_load)
        try:
            with self.SessionLocal() as db:
                files = list_completed_kiz_source_files(db)
                dates = list_completed_kiz_dates(db)
        finally:
            sa_event.remove(OrderItem, "load", record_load)

        self.assertEqual(len(files), 3)
        self.assertEqual(len(dates), 3)
        self.assertEqual(loaded, [])

    def test_lists_keep_completion_rules(self):
        # Запирающий: лёгкая выборка обязана читать те же признаки, что и ORM-путь:
        # короб из raw_payload скана, освобождённые блоки и закрытие без КИЗ
        import_id = self._seed_import()
        shipment = date(2026, 10, 14)
        ready_order = self._seed_order(shipment)
        self._seed_item(ready_order, import_id, "ready.xlsx", quantity=50, scans=[{"block_quantity": 50}])
        self._seed_item(ready_order, import_id, "ready.xlsx", quantity=3, scans=[{}], released=2)
        closed_order = self._seed_order(shipment, status="completed", raw_payload={"completed_without_kiz": True})
        self._seed_item(closed_order, import_id, "ready.xlsx", quantity=4, scans=[])
        pending_order = self._seed_order(shipment)
        self._seed_item(pending_order, import_id, "pending.xlsx", quantity=2, scans=[{}])

        with self.SessionLocal() as db:
            files = {entry["source_file"]: entry for entry in list_completed_kiz_source_files(db)}
            dates = list_completed_kiz_dates(db)

        self.assertEqual(
            {key: files["ready.xlsx"][key] for key in ("planned_blocks", "scanned_blocks", "remaining_blocks", "completed", "items", "dates")},
            {"planned_blocks": 53, "scanned_blocks": 51, "remaining_blocks": 2, "completed": True, "items": 3, "dates": ["2026-10-14"]},
        )
        self.assertEqual(files["ready.xlsx"]["source_key"], f"import:{import_id}:file:ready.xlsx")
        self.assertEqual(files["ready.xlsx"]["import_source"], "telegram")
        self.assertEqual(
            {key: files["pending.xlsx"][key] for key in ("planned_blocks", "scanned_blocks", "remaining_blocks", "completed")},
            {"planned_blocks": 2, "scanned_blocks": 1, "remaining_blocks": 1, "completed": False},
        )
        self.assertEqual(
            [{key: entry[key] for key in ("date", "items", "orders", "planned_blocks", "scanned_blocks", "completed")} for entry in dates],
            [{"date": "2026-10-14", "items": 2, "orders": 1, "planned_blocks": 55, "scanned_blocks": 52, "completed": False}],
        )

    def test_date_reports_load_only_items_of_requested_dates(self):
        own = {}
        for day in (12, 13, 14):
            own[day] = self._seed_file(f"day-{day}.xlsx", date(2026, 10, day), quantities=(1,))

        for label, build, expected_days in (
            ("date", lambda db: build_kiz_date_report_xlsx(db, "2026-10-13"), (13,)),
            ("range", lambda db: build_kiz_date_range_report_xlsx(db, "2026-10-12", "2026-10-13"), (12, 13)),
        ):
            with self.subTest(report=label):
                loaded = []

                def record_load(target, _context):
                    loaded.append(target.id)

                sa_event.listen(OrderItem, "load", record_load)
                try:
                    with self.SessionLocal() as db:
                        build(db)
                finally:
                    sa_event.remove(OrderItem, "load", record_load)

                self.assertEqual(sorted(loaded), sorted(item_id for day in expected_days for item_id in own[day]))

    def _seed_file(self, source_file, shipment, *, quantities):
        import_id = self._seed_import()
        order_id = self._seed_order(shipment)
        return [
            self._seed_item(order_id, import_id, source_file, quantity=quantity, scans=[{}] * quantity)
            for quantity in quantities
        ]

    def _seed_import(self):
        import_id = uuid.uuid4()
        with self.SessionLocal() as db:
            db.add(ImportJob(id=import_id, source="telegram", status="completed", rows_total=1, rows_imported=1, raw_payload={}))
            db.commit()
        return import_id

    def _seed_order(self, shipment, *, status="not_completed", raw_payload=None):
        order_id = uuid.uuid4()
        with self.SessionLocal() as db:
            db.add(Order(
                id=order_id, source="telegram", order_date=shipment, payment_type="Перечисление",
                client="Synthetic", address="Synthetic", status=status, raw_payload=raw_payload or {},
            ))
            db.commit()
        return order_id

    def _seed_item(self, order_id, import_id, source_file, *, quantity, scans, released=0):
        item_id = uuid.uuid4()
        raw_payload = {"backend_import_id": str(import_id), "source_file": source_file}
        if released:
            raw_payload["kiz_released_blocks"] = released
        with self.SessionLocal() as db:
            db.add(OrderItem(
                id=item_id, order_id=order_id, product="Synthetic", quantity_pieces=quantity * 10,
                quantity_blocks=quantity, scanned_blocks=len(scans), requires_kiz=True,
                status="not_completed", raw_payload=raw_payload,
            ))
            for scan_payload in scans:
                scan_id = uuid.uuid4()
                db.add(ScanCode(id=scan_id, order_item_id=item_id, code=f"01{scan_id.hex}", source="desktop", raw_payload=scan_payload))
            db.commit()
        return item_id


if __name__ == "__main__":
    unittest.main()
