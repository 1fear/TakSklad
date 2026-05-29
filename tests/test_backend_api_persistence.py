import unittest
import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.main import app, require_service_token
from backend.app.models import AuditLog, Base, Order, OrderItem, ScanCode


class BackendApiPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_service_token] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_order(self, *, status="not_completed", quantity_blocks=2, scanned_blocks=0, item_status="not_completed"):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Test Client",
                address="Test Address",
                representative="Test Rep",
                order_date=date(2026, 5, 30),
                status=status,
                raw_payload={"source": "test"},
            )
            item = OrderItem(
                order=order,
                product="Test Product",
                quantity_pieces=20,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                status=item_status,
                raw_payload={"source": "test"},
            )
            db.add_all([order, item])
            db.commit()
            return str(order.id), str(item.id)

    def test_active_orders_returns_uncompleted_orders_with_items(self):
        active_order_id, _ = self.seed_order()
        self.seed_order(status="completed", item_status="completed")

        response = self.client.get("/api/v1/orders/active")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], active_order_id)
        self.assertEqual(payload[0]["status"], "not_completed")
        self.assertEqual(payload[0]["items"][0]["product"], "Test Product")

    def test_scan_creates_code_increments_item_and_rejects_duplicate(self):
        _, item_id = self.seed_order()

        response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "  010123456789  ", "workstation_id": "pc-1"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["code"], "010123456789")
        self.assertEqual(payload["scanned_blocks"], 1)
        self.assertEqual(payload["item_status"], "not_completed")

        duplicate = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456789", "workstation_id": "pc-2"},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"], "Code already scanned")

        with self.SessionLocal() as db:
            self.assertEqual(len(db.execute(select(ScanCode)).scalars().all()), 1)
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)

    def test_complete_order_requires_required_blocks_and_closes_order(self):
        order_id, item_id = self.seed_order()

        too_early = self.client.post(f"/api/v1/orders/{order_id}/complete")

        self.assertEqual(too_early.status_code, 409)
        self.assertEqual(too_early.json()["detail"]["message"], "Order has incomplete required items")

        for code in ["010000000001", "010000000002"]:
            scan = self.client.post("/api/v1/scans", json={"order_item_id": item_id, "code": code})
            self.assertEqual(scan.status_code, 201)

        completed = self.client.post(f"/api/v1/orders/{order_id}/complete")

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "completed")
        self.assertEqual(completed.json()["items"][0]["status"], "completed")

        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json(), [])

        with self.SessionLocal() as db:
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertEqual(actions.count("scan_code_created"), 2)
            self.assertIn("order_completed", actions)


if __name__ == "__main__":
    unittest.main()
