import unittest
import uuid
from datetime import date

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.audit_identity import AuditActor, set_audit_actor
from backend.app.db import get_db
from backend.app.imports_service import normalize_import_row
from backend.app.main import (
    AuthContext,
    app,
    require_admin_write_permission,
    require_client_points_write_permission,
    require_service_token,
)
from backend.app.models import Base, Order, OrderItem, ScanCode
from backend.app.order_statuses import INACTIVE_ORDER_STATUSES


class KizLifecycleGuardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
        )

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        def override_auth(db=Depends(get_db)):
            set_audit_actor(
                db,
                AuditActor(
                    subject="service:test-kiz-lifecycle-guards",
                    service_principal_id=uuid.UUID(
                        "00000000-0000-0000-0000-000000000311"
                    ),
                ),
            )
            return AuthContext(
                login="test-kiz-lifecycle-guards",
                role="desktop",
                permissions=tuple(),
                source="service-principal",
                principal_id="00000000-0000-0000-0000-000000000311",
                token_id="00000000-0000-0000-0000-000000000322",
            )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_service_token] = override_auth
        app.dependency_overrides[require_admin_write_permission] = override_auth
        app.dependency_overrides[require_client_points_write_permission] = override_auth
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_order(
        self,
        *,
        status="not_completed",
        item_status=None,
        quantity_blocks=1,
        scanned_blocks=0,
        product="Synthetic Existing Product",
        import_order_key=None,
    ):
        with self.SessionLocal() as db:
            order = Order(
                source="synthetic_guard_test",
                external_id=import_order_key,
                import_order_key=import_order_key,
                import_source_order_key=import_order_key,
                payment_type="SYNTHETIC",
                client="Synthetic Guard Client",
                address="Synthetic Guard Address",
                representative="Synthetic Guard Representative",
                order_date=date(2026, 7, 30),
                status=status,
                raw_payload={
                    "source": "synthetic_guard_test",
                    "order_key": import_order_key or "",
                },
            )
            item = OrderItem(
                order=order,
                product=product,
                quantity_pieces=quantity_blocks * 10,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                requires_kiz=True,
                status=item_status or status,
                raw_payload={"source": "synthetic_guard_test"},
            )
            db.add_all([order, item])
            db.commit()
            return str(order.id), str(item.id)

    def add_scan(self, item_id, code):
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            item.scan_codes.append(
                ScanCode(
                    code=code,
                    source="synthetic_test",
                    raw_payload={"scan_type": "unit", "block_quantity": 1},
                )
            )
            db.commit()

    @staticmethod
    def import_row(label, product):
        return {
            "Дата отгрузки": "30.07.2026",
            "Тип оплаты": "SYNTHETIC",
            "Клиент": f"Synthetic Guard Client {label}",
            "Адрес": f"Synthetic Guard Address {label}",
            "Торговый представитель": "Synthetic Guard Representative",
            "Товары": product,
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "ID заказа": f"synthetic-guard-order-{label}",
            "ID импорта": f"synthetic-guard-item-{label}-{product}",
            "Ключ исходного документа": "synthetic-guard-batch",
        }

    # Spec: document §6.12 and §23 step 3, item 4.
    @unittest.expectedFailure
    def test_reset_rejects_completed_order(self):
        order_ids = {}
        for index, status in enumerate(INACTIVE_ORDER_STATUSES):
            order_id, item_id = self.seed_order(
                status=status,
                item_status=status,
                scanned_blocks=1,
            )
            self.add_scan(item_id, f"TEST-KIZ-RESET-{index}")
            order_ids[status] = order_id

        status_codes = {}
        for status, order_id in order_ids.items():
            response = self.client.post(
                f"/api/v1/admin/orders/{order_id}/reset-rescan",
                json={"reason": "Synthetic closed-order guard", "actor": "test"},
            )
            status_codes[status] = response.status_code

        self.assertEqual(
            status_codes,
            {status: 409 for status in INACTIVE_ORDER_STATUSES},
        )
        with self.SessionLocal() as db:
            for status, order_id in order_ids.items():
                order = db.get(Order, uuid.UUID(order_id))
                self.assertEqual(order.status, status)
                self.assertEqual(len(order.items[0].scan_codes), 1)

    def test_reset_allows_active_order(self):
        order_id, item_id = self.seed_order(scanned_blocks=1)
        self.add_scan(item_id, "TEST-KIZ-ACTIVE-RESET")

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/reset-rescan",
            json={"reason": "Synthetic active-order reset", "actor": "test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_completed")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(item.scan_codes, [])

    # Spec: document §6.11 and §23 step 3, item 4.
    @unittest.expectedFailure
    def test_complete_without_kiz_rejects_order_with_scans(self):
        order_id, item_id = self.seed_order(quantity_blocks=2, scanned_blocks=1)
        self.add_scan(item_id, "TEST-KIZ-COMPLETE-WITHOUT")

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [order_id],
                "reason": "Synthetic no-KIZ close",
                "actor": "test",
            },
        )

        self.assertEqual(response.status_code, 409)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.status, "not_completed")
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(len(item.scan_codes), 1)

    def test_complete_without_kiz_allows_order_without_scans(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [order_id],
                "reason": "Synthetic unscanned close",
                "actor": "test",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["completed"], 1)
        with self.SessionLocal() as db:
            self.assertEqual(db.get(Order, uuid.UUID(order_id)).status, "completed")
            self.assertEqual(db.get(OrderItem, uuid.UUID(item_id)).status, "completed")

    # Spec: document §13.1 and §23 step 3, item 5.
    @unittest.expectedFailure
    def test_import_does_not_add_position_to_inactive_order(self):
        rows = []
        inactive_orders = {}
        for status in INACTIVE_ORDER_STATUSES:
            row = self.import_row(status, f"Synthetic New Product {status}")
            order_key = normalize_import_row(row)["order_key"]
            order_id, _item_id = self.seed_order(
                status=status,
                item_status=status,
                import_order_key=order_key,
            )
            inactive_orders[status] = order_id
            rows.append(row)

        response = self.client.post(
            "/api/v1/imports",
            json={"source": "synthetic_guard_test", "rows": rows},
        )

        self.assertEqual(response.status_code, 201)
        with self.SessionLocal() as db:
            item_counts = {
                status: db.scalar(
                    select(func.count())
                    .select_from(OrderItem)
                    .where(OrderItem.order_id == uuid.UUID(order_id))
                )
                for status, order_id in inactive_orders.items()
            }
        self.assertEqual(
            item_counts,
            {status: 1 for status in INACTIVE_ORDER_STATUSES},
        )

    def test_import_adds_position_to_active_order(self):
        row = self.import_row("active", "Synthetic New Active Product")
        order_key = normalize_import_row(row)["order_key"]
        order_id, _item_id = self.seed_order(import_order_key=order_key)

        response = self.client.post(
            "/api/v1/imports",
            json={"source": "synthetic_guard_test", "rows": [row]},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["items_created"], 1)
        with self.SessionLocal() as db:
            item_count = db.scalar(
                select(func.count())
                .select_from(OrderItem)
                .where(OrderItem.order_id == uuid.UUID(order_id))
            )
            order_count = db.scalar(select(func.count()).select_from(Order))
        self.assertEqual(item_count, 2)
        self.assertEqual(order_count, 1)

    # Spec: document §6.19 and §23 step 3, item 6.
    @unittest.expectedFailure
    def test_complete_order_rejects_scanned_counter_without_scan_rows(self):
        order_id, item_id = self.seed_order(scanned_blocks=1)

        response = self.client.post(f"/api/v1/orders/{order_id}/complete")

        self.assertEqual(response.status_code, 409)
        with self.SessionLocal() as db:
            self.assertEqual(db.get(Order, uuid.UUID(order_id)).status, "not_completed")
            self.assertEqual(db.get(OrderItem, uuid.UUID(item_id)).status, "not_completed")

    def test_complete_order_allows_matching_scan_rows(self):
        order_id, item_id = self.seed_order(scanned_blocks=1)
        self.add_scan(item_id, "TEST-KIZ-COMPLETE-MATCH")

        response = self.client.post(f"/api/v1/orders/{order_id}/complete")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        with self.SessionLocal() as db:
            self.assertEqual(db.get(Order, uuid.UUID(order_id)).status, "completed")
            self.assertEqual(db.get(OrderItem, uuid.UUID(item_id)).status, "completed")
