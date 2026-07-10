import os
import unittest
import uuid
from datetime import date
from unittest import mock

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.imports_service import create_import
from backend.app.models import ImportJob, KizMovement, Order, OrderItem, PendingEvent, ScanCode
from backend.app.orders_service import complete_order, create_scan, mark_order_returned
from backend.app.schemas import ImportCreate, ScanCreate
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


class SyntheticOutboxFault(RuntimeError):
    pass


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresOutboxFaultTests(unittest.TestCase):
    database_name = "taksklad_phase9_outbox_faults"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def reset_database(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                TRUNCATE pending_events,audit_log,kiz_movements,kiz_codes,scan_codes,
                         order_items,orders,import_files,imports,incidents,client_points
                RESTART IDENTITY CASCADE
            """))

    def seed_order(self, *, status="not_completed", scanned_blocks=0, requires_kiz=True, with_scan=False):
        with self.SessionLocal() as session:
            order = Order(
                source="synthetic_phase9",
                external_id=f"phase9-{uuid.uuid4()}",
                payment_type="synthetic",
                client="SYNTHETIC PHASE9 CLIENT",
                address="SYNTHETIC PHASE9 ADDRESS",
                order_date=date(2026, 1, 15),
                status=status,
                raw_payload={},
            )
            item = OrderItem(
                order=order,
                product="Synthetic Product",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                requires_kiz=requires_kiz,
                status=status,
                raw_payload={},
            )
            session.add_all([order, item])
            session.flush()
            if with_scan:
                session.add(ScanCode(
                    order_item_id=item.id,
                    code=f"SYNTHETIC-PHASE9-{uuid.uuid4()}",
                    source="synthetic",
                    raw_payload={"scan_type": "unit", "block_quantity": 1},
                ))
            session.commit()
            return order.id, item.id

    def injected_fault(self, target_stage, producer):
        def fault(stage, actual_producer):
            if stage == target_stage and actual_producer == producer:
                raise SyntheticOutboxFault(f"synthetic {producer} {stage}")
        return fault

    def execute_case(self, producer, stage, invoke):
        side_effect = self.injected_fault(stage, producer) if stage in {"before_commit", "after_commit"} else None
        with self.SessionLocal() as session, mock.patch(
            "backend.app.outbox_service.outbox_fault",
            side_effect=side_effect,
        ):
            if stage in {"before_commit", "after_commit"}:
                with self.assertRaises(SyntheticOutboxFault):
                    invoke(session)
                session.rollback()
            else:
                invoke(session)

    def test_scan_complete_return_import_fault_matrix_has_zero_lost_intents(self):
        for producer in ("scan", "complete", "return", "import"):
            for stage in ("before_commit", "success", "after_commit"):
                with self.subTest(producer=producer, stage=stage):
                    self.reset_database()
                    if producer == "scan":
                        _order_id, item_id = self.seed_order()
                        code = f"SYNTHETIC-SCAN-{stage}"
                        self.execute_case(producer, stage, lambda session: create_scan(
                            session, ScanCreate(order_item_id=str(item_id), code=code)
                        ))
                        with self.SessionLocal() as observer:
                            mutation_count = observer.execute(select(ScanCode)).scalars().all()
                            item = observer.get(OrderItem, item_id)
                            events = observer.execute(select(PendingEvent)).scalars().all()
                        expected = 0 if stage == "before_commit" else 1
                        self.assertEqual(len(mutation_count), expected)
                        self.assertEqual(item.scanned_blocks, expected)
                        self.assertEqual(len(events), expected)
                        if events:
                            self.assertEqual(
                                (events[0].action, events[0].aggregate_type, events[0].aggregate_id),
                                ("google_sheets_scan_export", "order_item", str(item_id)),
                            )
                    elif producer == "complete":
                        order_id, _item_id = self.seed_order(requires_kiz=False)
                        self.execute_case(producer, stage, lambda session: complete_order(session, str(order_id)))
                        with self.SessionLocal() as observer:
                            order = observer.get(Order, order_id)
                            events = observer.execute(select(PendingEvent)).scalars().all()
                        expected = 0 if stage == "before_commit" else 1
                        self.assertEqual(order.status, "not_completed" if not expected else "completed")
                        self.assertEqual(len(events), expected)
                        if events:
                            self.assertEqual(
                                (events[0].action, events[0].aggregate_type, events[0].aggregate_id),
                                ("google_sheets_archive_export", "order", str(order_id)),
                            )
                    elif producer == "return":
                        order_id, item_id = self.seed_order(
                            status="completed", scanned_blocks=2, requires_kiz=True, with_scan=True,
                        )
                        confirmed = [{
                            "item_id": str(item_id),
                            "product": "Synthetic Product",
                            "quantity_blocks": 2,
                            "quantity_pieces": 20,
                        }]
                        self.execute_case(producer, stage, lambda session: mark_order_returned(
                            session,
                            str(order_id),
                            return_reference="SYNTHETIC-RETURN",
                            returned_by="synthetic",
                            confirmed_items=confirmed,
                        ))
                        with self.SessionLocal() as observer:
                            order = observer.get(Order, order_id)
                            movements = observer.execute(
                                select(KizMovement).where(KizMovement.movement_type == "return")
                            ).scalars().all()
                            events = observer.execute(select(PendingEvent)).scalars().all()
                        durable = stage != "before_commit"
                        self.assertEqual(order.status, "returned" if durable else "completed")
                        self.assertEqual(len(movements), 1 if durable else 0)
                        self.assertEqual(len(events), 3 if durable else 0)
                        if events:
                            self.assertEqual({event.action for event in events}, {
                                "skladbot_return_request_create",
                                "google_sheets_archive_export",
                                "google_sheets_return_export",
                            })
                            self.assertTrue(all(
                                event.aggregate_type == "order" and event.aggregate_id == str(order_id)
                                for event in events
                            ))
                    else:
                        payload = ImportCreate(
                            source="synthetic_phase9",
                            filename="synthetic-phase9.xlsx",
                            rows=[{
                                "Дата отгрузки": "15.01.2026",
                                "Тип оплаты": "synthetic",
                                "Клиент": "SYNTHETIC PHASE9 IMPORT",
                                "Адрес": "SYNTHETIC PHASE9 ADDRESS",
                                "Товары": "Synthetic Product",
                                "Кол-во ШТ": "20",
                                "Кол-во блок": "2",
                                "ID заказа": f"phase9-order-{stage}",
                                "ID импорта": f"phase9-item-{stage}",
                            }],
                        )
                        self.execute_case(producer, stage, lambda session: create_import(
                            session, payload, skladbot_create_mode="disabled",
                        ))
                        with self.SessionLocal() as observer:
                            imports = observer.execute(select(ImportJob)).scalars().all()
                            orders = observer.execute(select(Order)).scalars().all()
                            items = observer.execute(select(OrderItem)).scalars().all()
                            events = observer.execute(select(PendingEvent)).scalars().all()
                        expected = 0 if stage == "before_commit" else 1
                        self.assertEqual((len(imports), len(orders), len(items), len(events)), (expected,) * 4)
                        if events:
                            self.assertEqual(
                                (events[0].action, events[0].aggregate_type, events[0].aggregate_id),
                                ("google_sheets_import_export", "import", str(imports[0].id)),
                            )


if __name__ == "__main__":
    unittest.main()
