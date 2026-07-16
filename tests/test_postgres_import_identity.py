import os
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.imports_service import (
    active_order_predicate,
    create_import,
    normalize_import_row,
    source_import_lookup_key,
)
from backend.app.models import ImportFile, ImportJob, Order, OrderItem
from backend.app.schemas import ImportCreate
from tests.postgres_support import create_database, drop_database, run_alembic
from tools.import_identity_backfill import analyze, apply_backfill


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


def synthetic_row():
    return {
        "Дата отгрузки": "10.07.2026",
        "Тип оплаты": "SYNTHETIC",
        "Клиент": "SYNTHETIC CONCURRENT CLIENT",
        "Адрес": "SYNTHETIC CONCURRENT ADDRESS",
        "Товары": "SYNTHETIC CONCURRENT PRODUCT",
        "Кол-во ШТ": 20,
        "Кол-во блок": 2,
        "ID заказа": "synthetic-concurrent-order",
        "ID импорта": "synthetic-concurrent-import-row",
        "Ключ исходного документа": "synthetic-concurrent-batch",
    }


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresImportIdentityTests(unittest.TestCase):
    database_name = "taksklad_phase7_import_identity"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            for table in (
                "audit_log", "pending_events", "incidents", "client_points", "import_files",
                "imports", "order_items", "orders",
            ):
                connection.exec_driver_sql(f"TRUNCATE TABLE {table} CASCADE")

    def import_payload(self, *, sha="a" * 64):
        return ImportCreate(
            source="synthetic_postgres_test",
            filename="synthetic-concurrent.xlsx",
            sha256=sha,
            rows=[synthetic_row()],
        )

    def run_import(self, payload):
        with self.SessionLocal() as session:
            return create_import(session, payload)

    def test_two_concurrent_identical_imports_create_one_active_identity(self):
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(self.run_import(self.import_payload()))
            except Exception as exc:
                errors.append(exc)

        skladbot_result = {"status": "synthetic_stub", "ready": 0, "blocked": 0, "already_linked": 0,
                            "linked_mismatch": 0, "event_id": ""}
        with patch(
            "backend.app.imports_service.create_skladbot_dry_run_for_import",
            return_value=skladbot_result,
        ):
            threads = [threading.Thread(target=worker, daemon=True) for _index in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result.items_created for result in results), [0, 1])
        self.assertEqual(sorted(result.duplicate_rows for result in results), [0, 1])
        with self.SessionLocal() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(OrderItem)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(ImportFile)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(ImportJob)), 2)
            order = session.execute(select(Order)).scalar_one()
            item = session.execute(select(OrderItem)).scalar_one()
            jobs = session.execute(select(ImportJob)).scalars().all()
            import_file = session.execute(select(ImportFile)).scalar_one()

        normalized = normalize_import_row(synthetic_row())
        self.assertEqual(order.import_order_key, normalized["order_key"])
        self.assertEqual(order.import_source_order_key, normalized["order_key"])
        self.assertEqual(item.import_item_key, normalized["item_key"])
        self.assertEqual(item.source_import_id, normalized["source_import_id"])
        self.assertEqual(item.source_import_key, source_import_lookup_key(normalized["source_import_id"]))
        self.assertEqual(item.raw_payload["source_import_id"], item.source_import_id)
        replay_jobs = [
            job for job in jobs
            if (job.raw_payload or {}).get("file_sha256_reused_from_import_id")
        ]
        self.assertEqual(len(replay_jobs), 1)
        self.assertEqual(
            replay_jobs[0].raw_payload["file_sha256_reused_from_import_id"],
            str(import_file.import_id),
        )

    def test_returned_identity_does_not_block_reimport(self):
        skladbot_result = {"status": "synthetic_stub", "ready": 0, "blocked": 0, "already_linked": 0,
                            "linked_mismatch": 0, "event_id": ""}
        with patch(
            "backend.app.imports_service.create_skladbot_dry_run_for_import",
            return_value=skladbot_result,
        ):
            self.run_import(self.import_payload(sha="b" * 64))
            with self.SessionLocal() as session:
                order = session.execute(select(Order)).scalar_one()
                order.status = "completed"
                order.raw_payload = {**(order.raw_payload or {}), "return_status": "Returned"}
                session.commit()
            second = self.run_import(self.import_payload(sha="c" * 64))

        self.assertEqual(second.items_created, 1)
        with self.SessionLocal() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Order)), 2)
            active = session.scalar(select(func.count()).select_from(Order).where(active_order_predicate()))
            self.assertEqual(active, 1)

    def test_backfill_dry_run_is_bounded_resumable_and_reports_conflicts_without_mutation(self):
        with self.SessionLocal() as session:
            for index in range(2):
                order = Order(
                    source="synthetic_legacy", external_id=f"synthetic-legacy-{index}",
                    payment_type="SYNTHETIC", client=f"SYNTHETIC CLIENT {index}",
                    address=f"SYNTHETIC ADDRESS {index}", status="not_completed",
                    raw_payload={"order_key": f"synthetic-legacy-{index}"},
                )
                order.items.append(OrderItem(
                    product=f"SYNTHETIC PRODUCT {index}", quantity_pieces=10, quantity_blocks=1,
                    status="not_completed", raw_payload={
                        "item_key": str(index) * 64,
                        "source_import_id": "synthetic-conflicting-source-id",
                        "source_batch_key": "synthetic-conflict-batch",
                    },
                ))
                session.add(order)
            session.commit()

        first_page = analyze(self.url, batch_size=1, max_batches=1)
        self.assertFalse(first_page["orders_complete"])
        self.assertFalse(first_page["items_complete"])
        self.assertTrue(first_page["next_after_order_id"])
        self.assertTrue(first_page["next_after_item_id"])

        full = analyze(self.url, batch_size=1, max_batches=100)
        self.assertTrue(full["orders_complete"])
        self.assertTrue(full["items_complete"])
        self.assertTrue(any(
            conflict["type"] == "duplicate_active_item_identity"
            for conflict in full["conflicts"]
        ))
        with self.SessionLocal() as session:
            orders = session.execute(select(Order)).scalars().all()
            items = session.execute(select(OrderItem)).scalars().all()
        self.assertTrue(all(order.import_order_key is None for order in orders))
        self.assertTrue(all(item.source_import_key is None for item in items))

    def test_apply_backfill_honors_batch_cursors_and_resumes(self):
        with self.SessionLocal() as session:
            for index in range(3):
                order = Order(
                    source="synthetic_apply", external_id=f"synthetic-apply-{index}",
                    payment_type="SYNTHETIC", client=f"SYNTHETIC APPLY CLIENT {index}",
                    address=f"SYNTHETIC APPLY ADDRESS {index}", status="not_completed",
                    raw_payload={"order_key": f"synthetic-apply-{index}"},
                )
                item_raw = {"item_key": f"{index + 1}" * 64}
                if index < 2:
                    item_raw.update({
                        "source_import_id": f"synthetic-apply-row-{index}",
                        "source_batch_key": "synthetic-apply-batch",
                    })
                order.items.append(OrderItem(
                    product=f"SYNTHETIC APPLY PRODUCT {index}", quantity_pieces=10, quantity_blocks=1,
                    status="not_completed", raw_payload=item_raw,
                ))
                session.add(order)
            session.commit()

        cursors = {"after_order_id": "", "after_item_id": ""}
        total_orders = 0
        total_items = 0
        for _round in range(4):
            result = apply_backfill(
                self.url, batch_size=1, max_batches=1,
                after_order_id=cursors["after_order_id"], after_item_id=cursors["after_item_id"],
            )
            total_orders += result["updated_orders"]
            total_items += result["updated_items"]
            cursors = {
                "after_order_id": result["next_after_order_id"],
                "after_item_id": result["next_after_item_id"],
            }
            if result["orders_complete"] and result["items_complete"]:
                break

        self.assertEqual(total_orders, 3)
        self.assertEqual(total_items, 3)
        self.assertTrue(result["orders_complete"])
        self.assertTrue(result["items_complete"])
        with self.SessionLocal() as session:
            orders = session.execute(select(Order)).scalars().all()
            items = session.execute(select(OrderItem)).scalars().all()
        self.assertTrue(all(order.import_order_key for order in orders))
        self.assertEqual(sum(bool(item.source_import_key) for item in items), 2)

        repeated = apply_backfill(self.url, batch_size=10, max_batches=10)
        self.assertEqual(repeated["updated_orders"], 0)
        self.assertEqual(repeated["updated_items"], 0)


if __name__ == "__main__":
    unittest.main()
