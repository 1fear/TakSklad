import unittest
import uuid
from io import BytesIO
from datetime import date
from unittest import mock

import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.google_sheets_exporter import update_missing_sheet_addresses
from backend.app.main import app, require_service_token
from backend.app.models import AuditLog, Base, ImportJob, KizCode, KizMovement, Order, OrderItem, PendingEvent, ScanCode
from backend.app.skladbot_return_requests import SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE
from backend.app.settings import load_settings
from backend.app.web_auth import SESSION_COOKIE_NAME, hash_password


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

    def seed_order(
        self,
        *,
        status="not_completed",
        quantity_blocks=2,
        scanned_blocks=0,
        item_status="not_completed",
        product="Test Product",
    ):
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
                product=product,
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

    def confirmed_return_items(self, item_id, product="Test Product", blocks=2, pieces=20):
        return [{
            "item_id": item_id,
            "product": product,
            "sku": product,
            "quantity_blocks": blocks,
            "quantity_pieces": pieces,
        }]

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

    def test_admin_table_returns_flat_rows_totals_and_recent_activity(self):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Admin Client",
                address="Admin Address",
                representative="Admin Rep",
                order_date=date(2026, 6, 2),
                status="not_completed",
                raw_payload={
                    "coordinates": "41.2,69.2",
                    "skladbot_request_number": "SB-77",
                    "skladbot_request_id": "771",
                    "skladbot_status": "found",
                },
            )
            item = OrderItem(
                order=order,
                product="Admin Product",
                quantity_pieces=30,
                quantity_blocks=3,
                pieces_per_block=10,
                scanned_blocks=1,
                status="not_completed",
                raw_payload={
                    "source_file": "orders.xlsx",
                    "google_sheet_row_number": 12,
                    "google_sheet_synced_at": "2026-06-01T12:00:00+00:00",
                    "block_price": 240000,
                    "line_total": 720000,
                },
            )
            db.add(order)
            db.commit()
            item_id = str(item.id)
            order_id = str(order.id)
            db.add(PendingEvent(
                event_type="google_sheets_export",
                status="pending",
                payload={
                    "action": "google_sheets_scan_export",
                    "entity_type": "order_item",
                    "entity_id": item_id,
                },
            ))
            db.add(AuditLog(
                action="admin_test_activity",
                entity_type="order",
                entity_id=order_id,
                payload={"client": "Admin Client"},
            ))
            db.commit()

        response = self.client.get("/api/v1/admin/table")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["totals"]["orders"], 1)
        self.assertEqual(payload["totals"]["items"], 1)
        self.assertEqual(payload["totals"]["active_orders"], 1)
        self.assertEqual(payload["totals"]["planned_blocks"], 3)
        self.assertEqual(payload["totals"]["scanned_blocks"], 1)
        self.assertEqual(payload["totals"]["remaining_blocks"], 2)
        self.assertEqual(payload["totals"]["pending_google_exports"], 1)

        row = payload["rows"][0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["item_id"], item_id)
        self.assertEqual(row["status_bucket"], "active")
        self.assertEqual(row["client"], "Admin Client")
        self.assertEqual(row["product"], "Admin Product")
        self.assertEqual(row["source_file"], "orders.xlsx")
        self.assertEqual(row["google_sheet_status"], "pending")
        self.assertEqual(row["skladbot_request_number"], "SB-77")
        self.assertEqual(row["line_total"], 720000)
        self.assertEqual(payload["recent_activity"][0]["action"], "admin_test_activity")

    def test_admin_table_totals_are_not_limited_by_row_limit(self):
        first_order_id, _first_item_id = self.seed_order(quantity_blocks=3)
        second_order_id, _second_item_id = self.seed_order(quantity_blocks=4)

        response = self.client.get("/api/v1/admin/table?limit=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["totals"]["orders"], 2)
        self.assertEqual(payload["totals"]["items"], 2)
        self.assertEqual(payload["totals"]["planned_blocks"], 7)
        self.assertEqual({first_order_id, second_order_id}, {row["order_id"] for row in self.client.get("/api/v1/admin/table").json()["rows"]})

    def test_web_auth_login_sets_cookie_and_check_accepts_session(self):
        auth_settings = load_settings({
            "TAKSKLAD_ENV": "local",
            "TAKSKLAD_API_TOKEN": "service-token",
            "TAKSKLAD_WEB_LOGIN": "998000000000",
            "TAKSKLAD_WEB_PASSWORD_HASH": hash_password("test-password", salt="test-salt", iterations=1000),
            "TAKSKLAD_WEB_SESSION_SECRET": "test-session-secret",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        with mock.patch("backend.app.main.settings", auth_settings):
            session_before = self.client.get("/api/v1/auth/session")
            self.assertEqual(session_before.status_code, 200)
            self.assertFalse(session_before.json()["authenticated"])

            bad_login = self.client.post(
                "/api/v1/auth/login",
                json={"login": "998000000000", "password": "wrong-password"},
            )
            self.assertEqual(bad_login.status_code, 401)
            self.assertNotIn(SESSION_COOKIE_NAME, bad_login.cookies)

            login = self.client.post(
                "/api/v1/auth/login",
                json={"login": "998000000000", "password": "test-password"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json()["authenticated"])
            self.assertEqual(login.json()["login"], "998000000000")
            self.assertIn(SESSION_COOKIE_NAME, login.cookies)
            set_cookie = login.headers["set-cookie"]
            self.assertIn("HttpOnly", set_cookie)
            self.assertIn("SameSite=lax", set_cookie)

            check = self.client.get("/api/v1/auth/check")
            self.assertEqual(check.status_code, 204)

            logout = self.client.post("/api/v1/auth/logout")
            self.assertEqual(logout.status_code, 200)
            self.assertFalse(logout.json()["authenticated"])

            check_after_logout = self.client.get("/api/v1/auth/check")
            self.assertEqual(check_after_logout.status_code, 401)

    def test_web_auth_session_allows_admin_api_without_service_token(self):
        app.dependency_overrides.pop(require_service_token, None)
        self.seed_order()
        auth_settings = load_settings({
            "TAKSKLAD_ENV": "local",
            "TAKSKLAD_API_TOKEN": "service-token",
            "TAKSKLAD_WEB_LOGIN": "998000000000",
            "TAKSKLAD_WEB_PASSWORD_HASH": hash_password("test-password", salt="test-salt", iterations=1000),
            "TAKSKLAD_WEB_SESSION_SECRET": "test-session-secret",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        with mock.patch("backend.app.main.settings", auth_settings):
            unauthorized = self.client.get("/api/v1/admin/table")
            self.assertEqual(unauthorized.status_code, 401)

            login = self.client.post(
                "/api/v1/auth/login",
                json={"login": "998000000000", "password": "test-password"},
            )
            self.assertEqual(login.status_code, 200)

            admin = self.client.get("/api/v1/admin/table")
            self.assertEqual(admin.status_code, 200)
            self.assertEqual(len(admin.json()["rows"]), 1)

    def test_reset_order_for_rescan_clears_scans_and_keeps_order_active(self):
        order_id, item_id = self.seed_order(quantity_blocks=2, scanned_blocks=2, item_status="completed")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            item.scan_codes = [
                ScanCode(order_item_id=item.id, code="010000000001"),
                ScanCode(order_item_id=item.id, code="010000000002"),
            ]
            order = db.get(Order, uuid.UUID(order_id))
            order.status = "completed"
            db.commit()

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/reset-rescan",
            json={"actor": "anton"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_completed")
        self.assertEqual(response.json()["items"][0]["scanned_blocks"], 0)
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(len(db.execute(select(ScanCode)).scalars().all()), 0)
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_reset_for_rescan", actions)
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.payload["action"], "google_sheets_restore_order_export")

    def test_reset_order_for_rescan_rejects_returned_order(self):
        order_id, _item_id = self.seed_order(status="returned", item_status="returned")

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/reset-rescan",
            json={"reason": "Wrong order", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 409)

    def test_restore_order_returns_cancelled_order_to_active(self):
        order_id, _item_id = self.seed_order(status="cancelled", item_status="cancelled")

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/restore",
            json={"reason": "Wrong cancellation", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_completed")
        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()[0]["id"], order_id)
        with self.SessionLocal() as db:
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.payload["action"], "google_sheets_restore_order_export")
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_restored", actions)

    def test_resync_order_skladbot_keeps_cached_number_until_worker_updates_it(self):
        order_id, _item_id = self.seed_order()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_request_number": "WH-R-OLD",
                "skladbot_request_id": "OLD",
                "skladbot_status": "found",
            }
            db.commit()

        with mock.patch("backend.app.skladbot_worker.update_orders_from_skladbot", return_value={"status": "completed"}) as sync:
            response = self.client.post(
                f"/api/v1/admin/orders/{order_id}/resync-skladbot",
                json={"reason": "Retry match", "actor": "anton"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skladbot_request_number"], "WH-R-OLD")
        sync.assert_called_once()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-OLD")
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_skladbot_resync_requested", actions)

    def test_archive_without_kiz_moves_unscanned_order_out_of_active_without_marking_completed(self):
        order_id, item_id = self.seed_order(quantity_blocks=3)

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/archive-without-kiz",
            json={"reason": "Emergency close", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "archived_no_kiz")
        self.assertEqual(payload["items"][0]["status"], "archived_no_kiz")

        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json(), [])

        admin = self.client.get("/api/v1/admin/table")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(admin.json()["rows"][0]["status_bucket"], "archive_no_kiz")

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_archived_without_kiz", actions)
            self.assertNotIn("order_completed", actions)
            event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_archive_no_kiz_export")

    def test_archive_without_kiz_rejects_order_with_scans(self):
        order_id, _item_id = self.seed_order(scanned_blocks=1)

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/archive-without-kiz",
            json={"reason": "Emergency close", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "Order already has scanned KIZ codes")

    def test_bulk_complete_without_kiz_marks_unscanned_orders_completed_and_queues_archive_export(self):
        first_order_id, _first_item_id = self.seed_order(quantity_blocks=3)
        second_order_id, _second_item_id = self.seed_order(quantity_blocks=1)

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [first_order_id, second_order_id],
                "actor": "anton",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["completed"], 2)
        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json(), [])

        admin = self.client.get("/api/v1/admin/table")
        self.assertEqual(admin.status_code, 200)
        self.assertEqual({row["status_bucket"] for row in admin.json()["rows"]}, {"archive"})
        with self.SessionLocal() as db:
            orders = db.execute(select(Order)).scalars().all()
            self.assertEqual({order.status for order in orders}, {"completed"})
            items = db.execute(select(OrderItem)).scalars().all()
            self.assertEqual({item.status for item in items}, {"completed"})
            self.assertEqual({item.scanned_blocks for item in items}, {0})
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertEqual(actions.count("order_completed_without_kiz"), 2)
            events = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalars().all()
            self.assertEqual(len(events), 2)
            self.assertEqual({event.payload["action"] for event in events}, {"google_sheets_archive_export"})

    def test_bulk_complete_without_kiz_ignores_pending_skladbot_export(self):
        order_id, _item_id = self.seed_order(quantity_blocks=3)
        with self.SessionLocal() as db:
            db.add(PendingEvent(
                event_type="google_sheets_export",
                status="pending",
                payload={
                    "action": "google_sheets_skladbot_export",
                    "entity_id": "skladbot",
                    "order_ids": [order_id],
                },
            ))
            db.commit()

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [order_id],
                "actor": "anton",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["completed"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.status, "completed")

    def test_bulk_complete_without_kiz_rejects_partially_scanned_order_without_partial_changes(self):
        clean_order_id, _clean_item_id = self.seed_order(quantity_blocks=3)
        scanned_order_id, _scanned_item_id = self.seed_order(quantity_blocks=3, scanned_blocks=1)

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [clean_order_id, scanned_order_id],
                "reason": "Manual completed shipment",
                "actor": "anton",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "Bulk complete without KIZ rejected")
        self.assertEqual(response.json()["detail"]["errors"][0]["message"], "Order has partially scanned KIZ codes")
        with self.SessionLocal() as db:
            clean_order = db.get(Order, uuid.UUID(clean_order_id))
            scanned_order = db.get(Order, uuid.UUID(scanned_order_id))
            self.assertEqual(clean_order.status, "not_completed")
            self.assertEqual(scanned_order.status, "not_completed")
            self.assertEqual(db.execute(select(PendingEvent)).scalars().all(), [])

    def test_bulk_complete_without_kiz_allows_fully_scanned_order(self):
        order_id, _item_id = self.seed_order(quantity_blocks=2, scanned_blocks=2)

        response = self.client.post(
            "/api/v1/admin/orders/bulk/complete-without-kiz",
            json={
                "order_ids": [order_id],
                "actor": "anton",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["completed"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.execute(select(OrderItem).where(OrderItem.order_id == uuid.UUID(order_id))).scalar_one()
            self.assertEqual(order.status, "completed")
            self.assertEqual(item.status, "completed")
            self.assertEqual(item.scanned_blocks, 2)

    def test_cancel_order_requires_no_scans_and_queues_google_export_when_google_is_down(self):
        order_id, _item_id = self.seed_order(quantity_blocks=4)

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/cancel",
            json={"reason": "Wrong import", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        with self.SessionLocal() as db:
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_cancel_export")
            self.assertEqual(event.payload["entity_id"], order_id)
            self.assertEqual(event.last_error, "")

    def test_resync_google_for_active_order_pushes_items_without_archiving_order(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        response = self.client.post(
            f"/api/v1/admin/orders/{order_id}/resync-google",
            json={"reason": "Manual resync", "actor": "anton"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_completed")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.status, "not_completed")
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_google_resync_requested", actions)
            self.assertIn("google_sheets_scan_export", actions)
            event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
            self.assertEqual(event.payload["action"], "google_sheets_scan_export")
            self.assertEqual(event.payload["entity_id"], item_id)

    def test_sync_sources_runs_google_sheet_sync_then_skladbot_sync(self):
        with mock.patch("backend.app.main.sync_google_sheet_to_backend") as google_sync, mock.patch(
            "backend.app.main.update_orders_from_skladbot"
        ) as skladbot_sync:
            skladbot_sync.return_value = {
                "requests": 3,
                "updated": 2,
                "matched": 1,
                "not_found": 1,
                "multiple": 0,
            }

            response = self.client.post("/api/v1/sync/sources?wait_skladbot=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["google_sheets"]["status"], "skipped")
        self.assertEqual(payload["skladbot"]["status"], "completed")
        self.assertEqual(payload["skladbot"]["matched"], 1)
        google_sync.assert_not_called()
        skladbot_sync.assert_called_once()

    def test_sync_sources_can_skip_skladbot(self):
        with mock.patch("backend.app.main.sync_google_sheet_to_backend") as google_sync, mock.patch(
            "backend.app.main.update_orders_from_skladbot"
        ) as skladbot_sync:
            google_sync.return_value = {"rows": 1, "matched": 1, "orders_updated": 0, "items_updated": 0, "conflicts": 0}

            response = self.client.post("/api/v1/sync/sources?skladbot=0")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["skladbot"]["status"], "skipped")
        self.assertEqual(payload["google_sheets"]["status"], "skipped")
        google_sync.assert_not_called()
        skladbot_sync.assert_not_called()

    def test_sync_sources_starts_skladbot_in_background_by_default(self):
        with mock.patch("backend.app.main.sync_google_sheet_to_backend") as google_sync, mock.patch(
            "backend.app.main.start_skladbot_sync_background"
        ) as start_skladbot:
            google_sync.return_value = {"rows": 1, "matched": 1, "orders_updated": 0, "items_updated": 0, "conflicts": 0}
            start_skladbot.return_value = {"status": "started", "message": "background"}

            response = self.client.post("/api/v1/sync/sources")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["skladbot"]["status"], "started")
        self.assertEqual(payload["google_sheets"]["status"], "skipped")
        google_sync.assert_not_called()
        start_skladbot.assert_called_once()

    def test_scan_create_is_idempotent_for_same_item_and_rejects_cross_order_duplicate(self):
        _, item_id = self.seed_order()
        _, other_item_id = self.seed_order()

        response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "  010123456789  ", "workstation_id": "pc-1"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["code"], "010123456789")
        self.assertEqual(payload["scanned_blocks"], 1)
        self.assertEqual(payload["item_status"], "not_completed")

        same_item_duplicate = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456789", "workstation_id": "pc-2"},
        )
        self.assertEqual(same_item_duplicate.status_code, 201)
        self.assertEqual(same_item_duplicate.json()["order_item_id"], item_id)
        self.assertEqual(same_item_duplicate.json()["scanned_blocks"], 1)

        other_item_duplicate = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": other_item_id, "code": "010123456789", "workstation_id": "pc-2"},
        )
        self.assertEqual(other_item_duplicate.status_code, 409)
        self.assertEqual(
            other_item_duplicate.json()["detail"]["message"],
            "Code already scanned in another order item",
        )

        with self.SessionLocal() as db:
            self.assertEqual(len(db.execute(select(ScanCode)).scalars().all()), 1)
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)
            other_item = db.get(OrderItem, uuid.UUID(other_item_id))
            self.assertEqual(other_item.scanned_blocks, 0)

    def test_scan_create_is_idempotent_for_same_completed_item(self):
        _, item_id = self.seed_order(quantity_blocks=1)

        first = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456789", "workstation_id": "pc-1"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["item_status"], "completed")

        duplicate = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456789", "workstation_id": "pc-2"},
        )
        self.assertEqual(duplicate.status_code, 201)
        self.assertEqual(duplicate.json()["order_item_id"], item_id)
        self.assertEqual(duplicate.json()["scanned_blocks"], 1)

        with self.SessionLocal() as db:
            self.assertEqual(len(db.execute(select(ScanCode)).scalars().all()), 1)
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)
            self.assertEqual(item.status, "completed")

    def test_scan_create_acknowledges_extra_scan_when_item_already_full(self):
        _, item_id = self.seed_order(quantity_blocks=2)

        first = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456789", "workstation_id": "pc-1"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["item_status"], "not_completed")
        second = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010123456780", "workstation_id": "pc-1"},
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["scanned_blocks"], 2)
        self.assertEqual(second.json()["item_status"], "completed")

        extra = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010987654321", "workstation_id": "pc-1"},
        )

        self.assertEqual(extra.status_code, 201)
        self.assertEqual(extra.json()["order_item_id"], item_id)
        self.assertEqual(extra.json()["code"], "010123456780")
        self.assertEqual(extra.json()["scanned_blocks"], 2)
        self.assertEqual(extra.json()["item_status"], "completed")

        with self.SessionLocal() as db:
            scans = db.execute(select(ScanCode)).scalars().all()
            self.assertEqual(len(scans), 2)
            self.assertEqual({scan.code for scan in scans}, {"010123456789", "010123456780"})
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 2)
            self.assertEqual(item.status, "completed")

    def test_scan_create_counts_aggregate_box_as_fifty_blocks(self):
        _, item_id = self.seed_order(quantity_blocks=150, product="Chapman Gold SSL 100`20")

        response = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
                "workstation_id": "pc-1",
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["scanned_blocks"], 50)
        self.assertEqual(payload["item_status"], "not_completed")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            scan = db.execute(select(ScanCode)).scalar_one()
            self.assertEqual(item.scanned_blocks, 50)
            self.assertEqual(scan.raw_payload["scan_type"], "aggregate_box")
            self.assertEqual(scan.raw_payload["block_quantity"], 50)
            audit = db.execute(select(AuditLog).where(AuditLog.action == "scan_code_created")).scalar_one()
            self.assertEqual(audit.payload["scan_type"], "aggregate_box")
            self.assertEqual(audit.payload["block_quantity"], 50)

    def test_scan_create_rejects_aggregate_box_when_remaining_blocks_are_less_than_fifty(self):
        _, item_id = self.seed_order(quantity_blocks=30, product="Chapman Gold SSL 100`20")

        response = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "Aggregate box exceeds remaining order item blocks")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(db.execute(select(ScanCode)).scalars().all(), [])

    def test_scan_create_rejects_aggregate_box_for_wrong_product(self):
        _, item_id = self.seed_order(quantity_blocks=150, product="Chapman RED OP 20")

        response = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "Aggregate box product does not match order item")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(db.execute(select(ScanCode)).scalars().all(), [])

    def test_scan_create_rejects_unit_kiz_for_wrong_chapman_product(self):
        _, item_id = self.seed_order(quantity_blocks=1, product="Chapman Gold SSL 100`20")

        response = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "0104006396053947217p-30o933ZXHZKjx",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["message"], "Scan product does not match order item")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(db.execute(select(ScanCode)).scalars().all(), [])

    def test_scan_undo_subtracts_aggregate_box_block_quantity(self):
        _, item_id = self.seed_order(quantity_blocks=51, product="Chapman Gold SSL 100`20")
        aggregate = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
            },
        )
        unit = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010400639605400521UNIT"},
        )
        self.assertEqual(aggregate.status_code, 201)
        self.assertEqual(unit.status_code, 201)
        self.assertEqual(unit.json()["scanned_blocks"], 51)
        self.assertEqual(unit.json()["item_status"], "completed")

        undo_response = self.client.post(
            "/api/v1/scans/undo",
            json={
                "order_item_id": item_id,
                "code": "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
                "actor": "desktop",
            },
        )

        self.assertEqual(undo_response.status_code, 200)
        self.assertEqual(undo_response.json()["scanned_blocks"], 1)
        self.assertEqual(undo_response.json()["item_status"], "not_completed")
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)
            self.assertEqual([scan.code for scan in item.scan_codes], ["010400639605400521UNIT"])

    def test_scan_create_exports_scan_state_to_google_sheets_best_effort(self):
        _, item_id = self.seed_order()

        response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010000000001"},
        )

        self.assertEqual(response.status_code, 201)

        with self.SessionLocal() as db:
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("google_sheets_scan_export", actions)
            event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_scan_export")
            self.assertEqual(event.payload["entity_id"], item_id)

    def test_scan_undo_removes_backend_scan_and_queues_google_projection(self):
        _order_id, item_id = self.seed_order(quantity_blocks=1)
        create_response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010000000001", "workstation_id": "pc-1"},
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["item_status"], "completed")

        undo_response = self.client.post(
            "/api/v1/scans/undo",
            json={"order_item_id": item_id, "code": "010000000001", "workstation_id": "pc-1", "actor": "desktop"},
        )

        self.assertEqual(undo_response.status_code, 200)
        self.assertEqual(undo_response.json()["scanned_blocks"], 0)
        self.assertEqual(undo_response.json()["item_status"], "not_completed")
        with self.SessionLocal() as db:
            self.assertEqual(db.execute(select(ScanCode)).scalars().all(), [])
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(item.status, "not_completed")
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("scan_code_deleted", actions)
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalars().all()
            self.assertEqual([event.payload["action"] for event in events], ["google_sheets_scan_export"])

    def test_scan_undo_rejects_completed_order(self):
        order_id, item_id = self.seed_order(quantity_blocks=1)
        self.client.post("/api/v1/scans", json={"order_item_id": item_id, "code": "010000000001"})
        self.client.post(f"/api/v1/orders/{order_id}/complete")

        response = self.client.post(
            "/api/v1/scans/undo",
            json={"order_item_id": item_id, "code": "010000000001"},
        )

        self.assertEqual(response.status_code, 409)

    def test_scan_create_queues_google_sheets_export_when_google_is_down(self):
        _, item_id = self.seed_order()

        response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": "010000000901"},
        )

        self.assertEqual(response.status_code, 201)
        with self.SessionLocal() as db:
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_scan_export")
            self.assertEqual(event.payload["entity_id"], item_id)
            self.assertEqual(event.last_error, "")
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_scan_export")
            ).scalar_one()
            self.assertTrue(audit.payload["queued"])

    def test_sync_sources_flushes_pending_google_exports_before_google_refresh(self):
        _, item_id = self.seed_order()
        call_order = []
        with self.SessionLocal() as db:
            db.add(PendingEvent(
                event_type="google_sheets_export",
                status="pending",
                attempts=0,
                payload={
                    "action": "google_sheets_scan_export",
                    "entity_type": "order_item",
                    "entity_id": item_id,
                    "last_result": {"status": "error", "error": "Google timeout"},
                },
                last_error="Google timeout",
            ))
            db.commit()

        def fake_export(_items):
            call_order.append("pending_google_export")
            return {"status": "completed", "updated": 1}

        def fake_google_sync(_db):
            call_order.append("google_sheet_to_backend")
            return {"rows": 1, "matched": 1, "orders_updated": 0, "items_updated": 0, "conflicts": 0}

        with mock.patch(
            "backend.app.google_sheets_pending.sync_backend_order_items_to_google_sheets",
            side_effect=fake_export,
        ), mock.patch(
            "backend.app.main.sync_google_sheet_to_backend",
            side_effect=fake_google_sync,
        ):
            response = self.client.post("/api/v1/sync/sources?skladbot=0")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["google_sheets_pending"]["synced"], 1)
        self.assertEqual(payload["google_sheets"]["status"], "skipped")
        self.assertEqual(call_order, ["pending_google_export"])
        with self.SessionLocal() as db:
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.status, "completed")
            self.assertEqual(event.last_error, "")

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

    def test_complete_order_exports_archive_to_google_sheets_best_effort(self):
        order_id, item_id = self.seed_order()
        for code in ["010000000001", "010000000002"]:
            scan = self.client.post("/api/v1/scans", json={"order_item_id": item_id, "code": code})
            self.assertEqual(scan.status_code, 201)

        completed = self.client.post(f"/api/v1/orders/{order_id}/complete")

        self.assertEqual(completed.status_code, 200)

        with self.SessionLocal() as db:
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("google_sheets_archive_export", actions)
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalars().all()
            self.assertIn("google_sheets_archive_export", [event.payload["action"] for event in events])

    def test_return_lookup_and_mark_returned_excludes_order_from_active_list(self):
        order_id, item_id = self.seed_order(status="completed", scanned_blocks=2, item_status="completed")
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                "skladbot_request_number": "WR-RETURN-100",
                "skladbot_request_id": "100500",
            }
            db.commit()

        lookup = self.client.get("/api/v1/returns/lookup", params={"lookup": " WR-RETURN-100 "})

        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["id"], order_id)
        self.assertEqual(lookup.json()["skladbot_request_number"], "WR-RETURN-100")

        returned = self.client.post(
            f"/api/v1/returns/{order_id}",
            json={
                "return_reference": "WR-RETURN-100",
                "returned_by": "test",
                "confirmed_items": self.confirmed_return_items(item_id),
            },
        )

        self.assertEqual(returned.status_code, 200)
        self.assertEqual(returned.json()["status"], "returned")
        self.assertEqual(returned.json()["return_status"], "returned")
        self.assertEqual(returned.json()["return_reference"], "WR-RETURN-100")
        self.assertTrue(returned.json()["returned_at"])

        returns = self.client.get("/api/v1/returns")
        self.assertEqual(returns.status_code, 200)
        self.assertEqual(len(returns.json()), 1)
        self.assertEqual(returns.json()[0]["id"], order_id)
        self.assertEqual(returns.json()[0]["return_reference"], "WR-RETURN-100")

        duplicate_return = self.client.post(
            f"/api/v1/returns/{order_id}",
            json={"return_reference": "WR-RETURN-100", "returned_by": "test"},
        )
        self.assertEqual(duplicate_return.status_code, 409)
        self.assertEqual(duplicate_return.json()["detail"], "Order is already returned")

        lookup_after_return = self.client.get("/api/v1/returns/lookup", params={"lookup": "100500"})
        self.assertEqual(lookup_after_return.status_code, 200)
        self.assertEqual(lookup_after_return.json()["status"], "returned")
        self.assertEqual(lookup_after_return.json()["return_reference"], "WR-RETURN-100")

        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json(), [])

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.raw_payload["return_status"], "returned")
            self.assertEqual(order.raw_payload["return_reference"], "WR-RETURN-100")
            self.assertEqual(order.raw_payload["skladbot_return_confirmed_items"][0]["item_id"], item_id)
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()
            self.assertEqual(event.idempotency_key, f"skladbot:return_create:v1:order:{order_id}")
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("order_returned", actions)
            self.assertIn("skladbot_return_request_create_queued", actions)

    def test_return_releases_kiz_for_new_outbound_scan_with_history(self):
        first_order_id, first_item_id = self.seed_order(quantity_blocks=1)
        code = "01040000000000000001"

        first_scan = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": first_item_id, "code": code, "workstation_id": "pc-1"},
        )
        self.assertEqual(first_scan.status_code, 201)
        self.assertEqual(first_scan.json()["item_status"], "completed")
        self.assertEqual(self.client.post(f"/api/v1/orders/{first_order_id}/complete").status_code, 200)

        returned = self.client.post(
            f"/api/v1/returns/{first_order_id}",
            json={
                "return_reference": "WH-R-RETURN-200",
                "returned_by": "warehouse-pc",
                "confirmed_items": self.confirmed_return_items(first_item_id, blocks=1, pieces=20),
            },
        )
        self.assertEqual(returned.status_code, 200)

        second_order_id, second_item_id = self.seed_order(quantity_blocks=1)
        second_scan = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": second_item_id, "code": code, "workstation_id": "pc-2"},
        )

        self.assertEqual(second_scan.status_code, 201)
        self.assertEqual(second_scan.json()["order_item_id"], second_item_id)
        self.assertEqual(second_scan.json()["item_status"], "completed")

        with self.SessionLocal() as db:
            scan_rows = db.execute(
                select(ScanCode).where(ScanCode.code == code).order_by(ScanCode.scanned_at, ScanCode.id)
            ).scalars().all()
            self.assertEqual(len(scan_rows), 2)
            self.assertEqual({str(scan.order_item_id) for scan in scan_rows}, {first_item_id, second_item_id})

            kiz_codes = db.execute(select(KizCode).where(KizCode.code == code)).scalars().all()
            self.assertEqual(len(kiz_codes), 1)
            movements = db.execute(
                select(KizMovement)
                .where(KizMovement.kiz_id == kiz_codes[0].id)
                .order_by(KizMovement.occurred_at, KizMovement.id)
            ).scalars().all()
            self.assertEqual([movement.movement_type for movement in movements], ["outbound", "return", "re_outbound"])
            self.assertEqual(str(movements[-1].order_item_id), second_item_id)

            first_order = db.get(Order, uuid.UUID(first_order_id))
            second_order = db.get(Order, uuid.UUID(second_order_id))
            self.assertEqual(first_order.status, "returned")
            self.assertEqual(second_order.status, "not_completed")

    def test_scan_flushes_scan_code_before_kiz_movement(self):
        _order_id, item_id = self.seed_order(quantity_blocks=1)
        code = "01040000000000000003"

        from backend.app import orders_service

        original_record_kiz_movement = orders_service.record_kiz_movement

        def assert_scan_code_is_persisted(db, **kwargs):
            scan_code_id = kwargs["scan_code_id"]
            persisted_scan = db.execute(
                select(ScanCode).where(ScanCode.id == scan_code_id)
            ).scalar_one_or_none()
            if persisted_scan is None:
                raise AssertionError("scan_code must be flushed before kiz_movement")
            return original_record_kiz_movement(db, **kwargs)

        with mock.patch.object(orders_service, "record_kiz_movement", side_effect=assert_scan_code_is_persisted):
            response = self.client.post(
                "/api/v1/scans",
                json={"order_item_id": item_id, "code": code, "workstation_id": "pc-1"},
            )

        self.assertEqual(response.status_code, 201)

        with self.SessionLocal() as db:
            scan = db.execute(select(ScanCode).where(ScanCode.code == code)).scalar_one()
            movement = db.execute(
                select(KizMovement).join(KizCode, KizMovement.kiz_id == KizCode.id).where(KizCode.code == code)
            ).scalar_one()
            self.assertEqual(movement.scan_code_id, scan.id)

    def test_failed_return_does_not_release_kiz_for_new_order(self):
        first_order_id, first_item_id = self.seed_order(quantity_blocks=1)
        code = "01040000000000000002"
        self.assertEqual(
            self.client.post("/api/v1/scans", json={"order_item_id": first_item_id, "code": code}).status_code,
            201,
        )
        self.assertEqual(self.client.post(f"/api/v1/orders/{first_order_id}/complete").status_code, 200)

        failed_return = self.client.post(
            f"/api/v1/returns/{first_order_id}",
            json={
                "return_reference": "WH-R-RETURN-201",
                "returned_by": "warehouse-pc",
                "confirmed_items": self.confirmed_return_items(first_item_id, blocks=2, pieces=20),
            },
        )
        self.assertEqual(failed_return.status_code, 422)

        _second_order_id, second_item_id = self.seed_order(quantity_blocks=1)
        second_scan = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": second_item_id, "code": code},
        )
        self.assertEqual(second_scan.status_code, 409)

        with self.SessionLocal() as db:
            movements = db.execute(
                select(KizMovement).join(KizCode, KizMovement.kiz_id == KizCode.id).where(KizCode.code == code)
            ).scalars().all()
            self.assertEqual([movement.movement_type for movement in movements], ["outbound"])

    def test_mark_return_exports_archive_and_returns_to_google_sheets_best_effort(self):
        order_id, item_id = self.seed_order(status="completed", scanned_blocks=2, item_status="completed")
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                "skladbot_request_number": "WR-RETURN-101",
                "skladbot_request_id": "100501",
            }
            db.commit()

        returned = self.client.post(
            f"/api/v1/returns/{order_id}",
            json={
                "return_reference": "WR-RETURN-101",
                "returned_by": "test",
                "confirmed_items": self.confirmed_return_items(item_id),
            },
        )

        self.assertEqual(returned.status_code, 200)

        with self.SessionLocal() as db:
            actions = [row.action for row in db.execute(select(AuditLog)).scalars().all()]
            self.assertIn("google_sheets_archive_export", actions)
            self.assertIn("google_sheets_return_export", actions)
            event_actions = [
                event.payload["action"]
                for event in db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
                ).scalars().all()
            ]
            self.assertIn("google_sheets_archive_export", event_actions)
            self.assertIn("google_sheets_return_export", event_actions)

    def test_mark_return_rejects_mismatched_confirmed_items_without_side_effects(self):
        order_id, item_id = self.seed_order(status="completed", scanned_blocks=2, item_status="completed")

        returned = self.client.post(
            f"/api/v1/returns/{order_id}",
            json={
                "return_reference": "WR-RETURN-102",
                "returned_by": "test",
                "confirmed_items": self.confirmed_return_items(item_id, blocks=1),
            },
        )

        self.assertEqual(returned.status_code, 422)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.status, "completed")
            self.assertEqual(db.execute(select(PendingEvent)).scalars().all(), [])

    def test_import_creates_grouped_order_items_and_history(self):
        rows = [
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "cash",
                "Клиент": "Import Client",
                "Адрес": "Import Address",
                "Торговый представитель": "Import Rep",
                "Товары": "Product One",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "ID заказа": "source-order-1",
                "ID импорта": "import-row-1",
            },
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "cash",
                "Клиент": "Import Client",
                "Адрес": "Import Address",
                "Торговый представитель": "Import Rep",
                "Товары": "Product Two",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "source-order-2",
                "ID импорта": "import-row-2",
            },
        ]

        response = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["orders_created"], 1)
        self.assertEqual(payload["items_created"], 2)
        self.assertEqual(payload["duplicate_rows"], 0)
        self.assertEqual(payload["google_sheets_status"], "queued")
        self.assertEqual(payload["google_sheets_imported"], 0)

        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        active_payload = active.json()
        self.assertEqual(len(active_payload), 1)
        self.assertEqual(active_payload[0]["client"], "Import Client")
        self.assertEqual(len(active_payload[0]["items"]), 2)
        self.assertEqual(active_payload[0]["items"][0]["scan_codes"], [])

        history = self.client.get("/api/v1/imports")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()), 1)
        self.assertEqual(history.json()[0]["rows_imported"], 2)
        self.assertEqual(history.json()[0]["raw_payload"]["google_sheets"]["status"], "queued")
        with self.SessionLocal() as db:
            event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
            sheet_records = event.payload["records"]
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_import_export")
            self.assertEqual(len(sheet_records), 2)
            self.assertEqual(sheet_records[0]["Дата отгрузки"], "30.05.2026")
            self.assertEqual(sheet_records[0]["Клиент"], "Import Client")
            self.assertEqual(sheet_records[0]["ID импорта"], "import-row-1")

    def test_import_keeps_backend_data_when_google_sheets_export_fails(self):
        rows = [
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "cash",
                "Клиент": "Google Fail Client",
                "Адрес": "Import Address",
                "Товары": "Product One",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "ID заказа": "google-fail-order",
                "ID импорта": "google-fail-import",
            },
        ]

        response = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["items_created"], 1)
        self.assertEqual(payload["google_sheets_status"], "queued")
        self.assertEqual(payload["google_sheets_error"], "")

        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()[0]["client"], "Google Fail Client")
        with self.SessionLocal() as db:
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.payload["action"], "google_sheets_import_export")
            self.assertEqual(len(event.payload["records"]), 1)

    def test_duplicate_backend_import_still_can_backfill_google_sheets(self):
        row = {
            "Дата отгрузки": "30.05.2026",
            "Тип оплаты": "cash",
            "Клиент": "Backfill Client",
            "Адрес": "Import Address",
            "Товары": "Product One",
            "Кол-во ШТ": "20",
            "Кол-во блок": "2",
            "ID заказа": "backfill-order",
            "ID импорта": "backfill-import",
        }

        first = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": [row]})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["items_created"], 1)

        second = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": [row]})

        self.assertEqual(second.status_code, 201)
        payload = second.json()
        self.assertEqual(payload["items_created"], 0)
        self.assertEqual(payload["duplicate_rows"], 1)
        self.assertEqual(payload["google_sheets_status"], "queued")
        with self.SessionLocal() as db:
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalars().all()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1].payload["records"][0]["ID импорта"], "backfill-import")

    def test_changed_address_with_same_source_import_id_does_not_create_backend_duplicate(self):
        first_row = {
            "Дата отгрузки": "30.05.2026",
            "Тип оплаты": "cash",
            "Клиент": "Stable Import Client",
            "Адрес": "Адрес не указан",
            "Товары": "Product One",
            "Кол-во ШТ": "20",
            "Кол-во блок": "2",
            "ID заказа": "stable-order",
            "ID импорта": "stable-import",
        }
        second_row = {
            **first_row,
            "Адрес": "Ташкент, Чиланзарский район, 10",
        }

        first = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": [first_row]})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["items_created"], 1)

        second = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": [second_row]})

        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["items_created"], 0)
        self.assertEqual(second.json()["duplicate_rows"], 1)
        self.assertEqual(second.json()["backend_address_updates"], 1)
        self.assertEqual(second.json()["google_sheets_status"], "queued")
        with self.SessionLocal() as db:
            orders = db.execute(select(Order)).scalars().all()
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(db.execute(select(OrderItem)).scalars().all()), 1)
            self.assertEqual(orders[0].address, "Ташкент, Чиланзарский район, 10")
            self.assertEqual(orders[0].raw_payload["address_backfill_source"], "import")

    def test_google_sheets_export_updates_missing_address_for_existing_import_id(self):
        class FakeSheet:
            def __init__(self):
                self.updates = []

            def batch_update(self, updates, value_input_option=None):
                self.updates.extend(updates)
                self.value_input_option = value_input_option

        rows = [
            ["Дата отгрузки", "Тип оплаты", "Клиент", "Адрес"] + [""] * 22 + ["ID заказа", "ID импорта"],
            ["30.05.2026", "cash", "Backfill Client", "Адрес не указан"] + [""] * 22 + ["order-1", "import-1"],
        ]
        records = [{
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Адрес": "Ташкент, Чиланзарский район, 10",
        }]
        sheet = FakeSheet()

        updated = update_missing_sheet_addresses(sheet, rows, records)

        self.assertEqual(updated, 1)
        self.assertEqual(sheet.updates, [{"range": "D2", "values": [["Ташкент, Чиланзарский район, 10"]]}])
        self.assertEqual(sheet.value_input_option, "USER_ENTERED")

    def test_google_sheets_export_updates_not_found_address_for_existing_import_id(self):
        class FakeSheet:
            def __init__(self):
                self.updates = []

            def batch_update(self, updates, value_input_option=None):
                self.updates.extend(updates)
                self.value_input_option = value_input_option

        rows = [
            ["Дата отгрузки", "Тип оплаты", "Клиент", "Адрес"] + [""] * 22 + ["ID заказа", "ID импорта"],
            ["30.05.2026", "cash", "Backfill Client", "Адрес не найден"] + [""] * 22 + ["order-1", "import-1"],
        ]
        records = [{
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Адрес": "Ташкент, Чиланзарский район, 10",
        }]
        sheet = FakeSheet()

        updated = update_missing_sheet_addresses(sheet, rows, records)

        self.assertEqual(updated, 1)
        self.assertEqual(sheet.updates, [{"range": "D2", "values": [["Ташкент, Чиланзарский район, 10"]]}])

    def test_google_sheets_export_updates_coordinate_address_by_business_key_when_ids_changed(self):
        class FakeSheet:
            def __init__(self):
                self.updates = []

            def batch_update(self, updates, value_input_option=None):
                self.updates.extend(updates)
                self.value_input_option = value_input_option

        rows = [
            [
                "Дата отгрузки",
                "Тип оплаты",
                "Клиент",
                "Адрес",
                "Торговый представитель",
                "Товары",
                "Кол-во ШТ",
                "Кол-во блок",
            ] + [""] * 18 + ["ID заказа", "ID импорта"],
            [
                "04.06.2026",
                "Терминал",
                "Backfill Client",
                "Координаты: 41.325341, 69.233731",
                "ТП2",
                "Chapman Brown OP 20",
                "500",
                "50",
            ] + [""] * 18 + ["old-order", "old-import"],
        ]
        records = [{
            "Дата отгрузки": "04.06.2026",
            "Тип оплаты": "Терминал",
            "Клиент": "Backfill Client",
            "Адрес": "Ташкент, улица Сакичмон, 1/18",
            "Торговый представитель": "ТП2",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 500,
            "Кол-во блок": 50,
            "ID заказа": "new-order",
            "ID импорта": "new-import",
        }]
        sheet = FakeSheet()

        updated = update_missing_sheet_addresses(sheet, rows, records)

        self.assertEqual(updated, 1)
        self.assertEqual(sheet.updates, [{"range": "D2", "values": [["Ташкент, улица Сакичмон, 1/18"]]}])
        self.assertEqual(rows[1][3], "Ташкент, улица Сакичмон, 1/18")

    def test_google_sheets_export_does_not_update_ambiguous_business_key_address(self):
        class FakeSheet:
            def __init__(self):
                self.updates = []

            def batch_update(self, updates, value_input_option=None):
                self.updates.extend(updates)

        rows = [
            [
                "Дата отгрузки",
                "Тип оплаты",
                "Клиент",
                "Адрес",
                "Торговый представитель",
                "Товары",
                "Кол-во ШТ",
                "Кол-во блок",
            ] + [""] * 18 + ["ID заказа", "ID импорта"],
            [
                "04.06.2026",
                "Терминал",
                "Backfill Client",
                "Координаты: 41.325341, 69.233731",
                "ТП2",
                "Chapman Brown OP 20",
                "500",
                "50",
            ] + [""] * 18 + ["old-order-1", "old-import-1"],
            [
                "04.06.2026",
                "Терминал",
                "Backfill Client",
                "Координаты: 41.325341, 69.233731",
                "ТП2",
                "Chapman Brown OP 20",
                "500",
                "50",
            ] + [""] * 18 + ["old-order-2", "old-import-2"],
        ]
        records = [{
            "Дата отгрузки": "04.06.2026",
            "Тип оплаты": "Терминал",
            "Клиент": "Backfill Client",
            "Адрес": "Ташкент, улица Сакичмон, 1/18",
            "Торговый представитель": "ТП2",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 500,
            "Кол-во блок": 50,
            "ID заказа": "new-order",
            "ID импорта": "new-import",
        }]
        sheet = FakeSheet()

        updated = update_missing_sheet_addresses(sheet, rows, records)

        self.assertEqual(updated, 0)
        self.assertEqual(sheet.updates, [])

    def test_import_stores_coordinates_blocks_and_prices(self):
        rows = [
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "Терминал",
                "Клиент": "Price Client",
                "Адрес": "Tashkent Address",
                "Координаты": "41.31, 69.27",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "200",
                "Сумма позиции": "4800000",
                "ID заказа": "price-source-order",
            },
        ]

        response = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})

        self.assertEqual(response.status_code, 201)
        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        order = active.json()[0]
        self.assertEqual(order["coordinates"], "41.31, 69.27")
        self.assertEqual(order["items"][0]["quantity_pieces"], 200)
        self.assertEqual(order["items"][0]["quantity_blocks"], 20)
        self.assertEqual(order["items"][0]["block_price"], 240000)
        self.assertEqual(order["items"][0]["line_total"], 4_800_000)

    def test_import_marks_missing_address_as_pickup(self):
        rows = [
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "Терминал",
                "Клиент": "Pickup Import Client",
                "Адрес": "",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "20",
                "ID заказа": "pickup-import-order",
            },
        ]

        response = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})

        self.assertEqual(response.status_code, 201)
        active = self.client.get("/api/v1/orders/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()[0]["address"], "Самовывоз со склада")

    def test_import_skips_duplicate_items_and_reports_invalid_rows(self):
        valid_row = {
            "Дата отгрузки": "2026-05-30",
            "Тип оплаты": "cash",
            "Клиент": "Duplicate Client",
            "Адрес": "Duplicate Address",
            "Товары": "Duplicate Product",
            "Кол-во ШТ": 20,
            "Кол-во блок": 2,
            "ID заказа": "duplicate-source-order",
        }
        first = self.client.post("/api/v1/imports", json={"source": "excel", "rows": [valid_row]})
        second = self.client.post("/api/v1/imports", json={"source": "excel", "rows": [valid_row, {"Клиент": "Broken"}]})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        payload = second.json()
        self.assertEqual(payload["items_created"], 0)
        self.assertEqual(payload["duplicate_rows"], 1)
        self.assertEqual(payload["invalid_rows"], 1)
        self.assertEqual(payload["status"], "failed")

        with self.SessionLocal() as db:
            self.assertEqual(len(db.execute(select(Order)).scalars().all()), 1)
            self.assertEqual(len(db.execute(select(OrderItem)).scalars().all()), 1)

    def test_day_report_summarizes_orders_scans_and_payment_groups(self):
        rows = [
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "Терминал",
                "Клиент": "Report Client",
                "Адрес": "Report Address",
                "Торговый представитель": "Report Rep",
                "Товары": "Report Product One",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "Номер заявки SkladBot": "WR-100",
                "ID заказа": "report-source-order-1",
            },
            {
                "Дата отгрузки": "30.05.2026",
                "Тип оплаты": "Терминал",
                "Клиент": "Report Client",
                "Адрес": "Report Address",
                "Торговый представитель": "Report Rep",
                "Товары": "Report Product Two",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "Номер заявки SkladBot": "WR-100",
                "ID заказа": "report-source-order-2",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        active = self.client.get("/api/v1/orders/active").json()
        order_id = active[0]["id"]
        item_ids = {item["product"]: item["id"] for item in active[0]["items"]}

        scans = [
            ("Report Product One", "010000000101"),
            ("Report Product One", "010000000102"),
            ("Report Product Two", "010000000201"),
        ]
        for product, code in scans:
            response = self.client.post(
                "/api/v1/scans",
                json={
                    "order_item_id": item_ids[product],
                    "code": code,
                    "scanned_at": "2026-05-30T12:00:00+00:00",
                },
            )
            self.assertEqual(response.status_code, 201)

        active_after_scans = self.client.get("/api/v1/orders/active")
        self.assertEqual(active_after_scans.status_code, 200)
        active_item = active_after_scans.json()[0]["items"][0]
        self.assertTrue(active_item["scan_codes"])

        completed = self.client.post(f"/api/v1/orders/{order_id}/complete")
        self.assertEqual(completed.status_code, 200)

        report = self.client.get("/api/v1/reports/day?report_date=2026-05-30")

        self.assertEqual(report.status_code, 200)
        payload = report.json()
        self.assertEqual(payload["report_date"], "2026-05-30")
        self.assertEqual(payload["source"], "postgres")
        self.assertEqual(payload["totals"]["orders"], 1)
        self.assertEqual(payload["totals"]["completed_orders"], 1)
        self.assertEqual(payload["totals"]["active_orders"], 0)
        self.assertEqual(payload["totals"]["items"], 2)
        self.assertEqual(payload["totals"]["completed_items"], 2)
        self.assertEqual(payload["totals"]["planned_blocks"], 3)
        self.assertEqual(payload["totals"]["scanned_blocks"], 3)
        self.assertEqual(payload["totals"]["scanned_today"], 3)
        self.assertEqual(payload["totals"]["remaining_blocks"], 0)
        self.assertEqual(payload["totals"]["scan_codes"], 3)
        self.assertEqual(payload["payment_groups"][0]["payment_group"], "terminal")
        self.assertEqual(payload["payment_groups"][0]["orders"], 1)
        self.assertEqual(payload["orders"][0]["skladbot_request_number"], "WR-100")

    def test_day_report_counts_scan_by_business_timezone(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-31",
                "Тип оплаты": "Терминал",
                "Клиент": "Timezone Client",
                "Адрес": "Tashkent Address",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "timezone-source-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        active = self.client.get("/api/v1/orders/active").json()
        item_id = active[0]["items"][0]["id"]
        scan = self.client.post(
            "/api/v1/scans",
            json={
                "order_item_id": item_id,
                "code": "0104006396053978217TIMEZONE001",
                "scanned_at": "2026-05-31T20:30:00+00:00",
            },
        )
        self.assertEqual(scan.status_code, 201)

        with mock.patch.dict("os.environ", {"TAKSKLAD_TIMEZONE": "Asia/Tashkent"}):
            report = self.client.get("/api/v1/reports/day?report_date=2026-06-01")

        self.assertEqual(report.status_code, 200)
        payload = report.json()
        self.assertEqual(payload["report_date"], "2026-06-01")
        self.assertEqual(payload["totals"]["orders"], 1)
        self.assertEqual(payload["totals"]["scanned_today"], 1)
        self.assertEqual(payload["orders"][0]["client"], "Timezone Client")

    def test_logistics_report_uses_shipment_date_coordinates_and_prices(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Logistics Client",
                "Адрес": "Tashkent Address",
                "Координаты": "41.31, 69.27",
                "Торговый представитель": "Rep One",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "200",
                "Кол-во блок": "20",
                "Сумма позиции": "4800000",
                "ID заказа": "logistics-source-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        dates = self.client.get("/api/v1/logistics/dates")
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json(), ["2026-05-30"])

        report = self.client.get("/api/v1/logistics/report?shipment_date=2026-05-30")
        self.assertEqual(report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(report.content), data_only=True)
        sheet = workbook["Заявки"]

        self.assertEqual(sheet["C2"].value, "Logistics Client")
        self.assertEqual(sheet["G2"].value, "41.31,69.27")
        self.assertEqual(sheet["J2"].value, "30.05.2026")
        self.assertEqual(sheet["R2"].value, "Chapman Brown OP 20")
        self.assertEqual(sheet["S2"].value, 20)
        self.assertEqual(sheet["V2"].value, 240000)
        self.assertEqual(sheet["W2"].value, 4_800_000)
        self.assertEqual(sheet["AE2"].value, "41.31,69.27")
        self.assertEqual(sheet["AF2"].value, "41.31")
        self.assertEqual(sheet["AG2"].value, "69.27")
        workbook.close()

    def test_logistics_report_skips_pickup_and_orders_without_coordinates(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Route Client",
                "Адрес": "Tashkent Address",
                "Координаты": "41.31, 69.27",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "ID заказа": "route-order",
            },
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "No Coordinates Client",
                "Адрес": "Tashkent Address Without Coordinates",
                "Товары": "Chapman RED OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "no-coordinates-order",
            },
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Pickup Client",
                "Адрес": "Самовывоз со склада",
                "Координаты": "41.32, 69.28",
                "Товары": "Chapman Gold SSL",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "pickup-order",
            },
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Invalid Coordinates Client",
                "Адрес": "Invalid Coordinates Address",
                "Координаты": "999, 999",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "invalid-coordinates-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        dates = self.client.get("/api/v1/logistics/dates")
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json(), ["2026-05-30"])

        report = self.client.get("/api/v1/logistics/report?shipment_date=2026-05-30")
        self.assertEqual(report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(report.content), data_only=True)
        sheet = workbook["Заявки"]

        self.assertEqual(sheet.max_row, 2)
        self.assertEqual(sheet["C2"].value, "Route Client")
        self.assertEqual(sheet["R2"].value, "Chapman Brown OP 20")
        workbook.close()

    def test_logistics_report_404_when_date_has_only_pickup_or_unrouteable_orders(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Pickup Client",
                "Адрес": "Самовывоз со склада",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "ID заказа": "pickup-only-order",
            },
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "No Coordinates Client",
                "Адрес": "Tashkent Address Without Coordinates",
                "Товары": "Chapman RED OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "no-coordinates-only-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        dates = self.client.get("/api/v1/logistics/dates")
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json(), [])

        report = self.client.get("/api/v1/logistics/report?shipment_date=2026-05-30")

        self.assertEqual(report.status_code, 404)
        self.assertIn("No logistics delivery orders", report.json()["detail"])

    def test_logistics_report_normalizes_three_part_coordinates(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Coordinates Client",
                "Адрес": "Tashkent Address",
                "Координаты": "41.214609,69.223027,15",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "ID заказа": "coordinates-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        report = self.client.get("/api/v1/logistics/report?shipment_date=2026-05-30")
        self.assertEqual(report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(report.content), data_only=True)
        sheet = workbook["Заявки"]

        self.assertEqual(sheet["AE2"].value, "41.214609,69.223027")
        self.assertEqual(sheet["AF2"].value, "41.214609")
        self.assertEqual(sheet["AG2"].value, "69.223027")
        workbook.close()

    def test_kiz_reports_show_source_file_progress_and_allow_partial_date_export(self):
        rows = [
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "KIZ Client",
                "Адрес": "KIZ Address",
                "Координаты": "41.31, 69.27",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "Сумма позиции": "480000",
                "Источник файла": "source-a.xlsx",
                "ID заказа": "kiz-source-order",
            },
            {
                "Дата отгрузки": "2026-05-30",
                "Тип оплаты": "Терминал",
                "Клиент": "Partial Date Client",
                "Адрес": "Partial Address",
                "Товары": "Chapman RED OP 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "Источник файла": "source-c.xlsx",
                "ID заказа": "partial-date-order",
            },
            {
                "Дата отгрузки": "2026-05-31",
                "Тип оплаты": "Перечисление",
                "Клиент": "Open Client",
                "Адрес": "Open Address",
                "Товары": "Chapman Gold SSL 20",
                "Кол-во ШТ": "10",
                "Кол-во блок": "1",
                "Источник файла": "source-b.xlsx",
                "ID заказа": "open-source-order",
            },
        ]
        imported = self.client.post("/api/v1/imports", json={"source": "excel", "filename": "orders.xlsx", "rows": rows})
        self.assertEqual(imported.status_code, 201)

        active = self.client.get("/api/v1/orders/active").json()
        kiz_order = next(order for order in active if order["client"] == "KIZ Client")
        item_id = kiz_order["items"][0]["id"]
        for code in ("0104006396053978217SOURCEA001", "0104006396053978217SOURCEA002"):
            response = self.client.post("/api/v1/scans", json={"order_item_id": item_id, "code": code})
            self.assertEqual(response.status_code, 201)

        source_files = self.client.get("/api/v1/reports/kiz/source-files")
        self.assertEqual(source_files.status_code, 200)
        source_payload = source_files.json()
        by_file = {item["source_file"]: item for item in source_payload}
        self.assertEqual(set(by_file), {"source-a.xlsx", "source-b.xlsx", "source-c.xlsx"})
        self.assertTrue(by_file["source-a.xlsx"]["source_key"].startswith("import:"))
        self.assertTrue(by_file["source-a.xlsx"]["completed"])
        self.assertEqual(by_file["source-a.xlsx"]["scanned_blocks"], 2)
        self.assertEqual(by_file["source-a.xlsx"]["planned_blocks"], 2)
        self.assertFalse(by_file["source-b.xlsx"]["completed"])
        self.assertEqual(by_file["source-b.xlsx"]["scanned_blocks"], 0)
        self.assertEqual(by_file["source-b.xlsx"]["planned_blocks"], 1)
        self.assertFalse(by_file["source-c.xlsx"]["completed"])

        report = self.client.get(
            "/api/v1/reports/kiz/source-file",
            params={"source_file": "source-a.xlsx", "source_key": by_file["source-a.xlsx"]["source_key"]},
        )
        self.assertEqual(report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(report.content), data_only=True)
        summary = workbook["Сводка"]
        self.assertEqual(summary["C2"].value, "KIZ Client")
        self.assertEqual(summary["G2"].value, 2)
        self.assertEqual(summary["H2"].value, 2)
        self.assertEqual(summary["I2"].value, 480000)
        sheet = workbook["Терминал"]
        self.assertEqual(sheet["C2"].value, "KIZ Client")
        self.assertEqual(sheet["G2"].value, "Chapman Brown OP 20")
        self.assertEqual(sheet["H2"].value, 1)
        self.assertEqual(sheet["I2"].value, "0104006396053978217SOURCEA001")
        self.assertEqual(sheet["H3"].value, 1)
        self.assertEqual(sheet["I3"].value, "0104006396053978217SOURCEA002")
        self.assertEqual(sheet["K2"].value, "source-a.xlsx")
        workbook.close()

        dates = self.client.get("/api/v1/reports/kiz/dates")
        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json()[0]["date"], "2026-05-30")
        self.assertFalse(dates.json()[0]["completed"])
        self.assertEqual(dates.json()[0]["planned_blocks"], 3)
        self.assertEqual(dates.json()[0]["scanned_blocks"], 2)

        date_report = self.client.get("/api/v1/reports/kiz/date", params={"shipment_date": "2026-05-30"})
        self.assertEqual(date_report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(date_report.content), data_only=True)
        summary = workbook["Сводка"]
        self.assertEqual(summary["C2"].value, "KIZ Client")
        self.assertEqual(summary["G2"].value, 2)
        self.assertEqual(summary["H2"].value, 2)
        workbook.close()

    def test_kiz_source_file_report_separates_same_filename_by_import(self):
        first_import = self.client.post(
            "/api/v1/imports",
            json={
                "source": "excel",
                "filename": "same-name.xlsx",
                "rows": [
                    {
                        "Дата отгрузки": "2026-05-30",
                        "Тип оплаты": "Терминал",
                        "Клиент": "Done Client",
                        "Адрес": "Done Address",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": "10",
                        "Кол-во блок": "1",
                        "Источник файла": "same-name.xlsx",
                        "ID заказа": "same-done-order",
                    },
                ],
            },
        )
        self.assertEqual(first_import.status_code, 201)

        active = self.client.get("/api/v1/orders/active").json()
        done_order = next(order for order in active if order["client"] == "Done Client")
        response = self.client.post(
            "/api/v1/scans",
            json={"order_item_id": done_order["items"][0]["id"], "code": "0104006396053978217SAMENAME001"},
        )
        self.assertEqual(response.status_code, 201)

        second_import = self.client.post(
            "/api/v1/imports",
            json={
                "source": "excel",
                "filename": "same-name.xlsx",
                "rows": [
                    {
                        "Дата отгрузки": "2026-05-30",
                        "Тип оплаты": "Перечисление",
                        "Клиент": "Open Client",
                        "Адрес": "Open Address",
                        "Товары": "Chapman Gold SSL 20",
                        "Кол-во ШТ": "10",
                        "Кол-во блок": "1",
                        "Источник файла": "same-name.xlsx",
                        "ID заказа": "same-open-order",
                    },
                ],
            },
        )
        self.assertEqual(second_import.status_code, 201)

        source_files = self.client.get("/api/v1/reports/kiz/source-files")
        self.assertEqual(source_files.status_code, 200)
        same_name_files = [item for item in source_files.json() if item["source_file"] == "same-name.xlsx"]
        self.assertEqual(len(same_name_files), 2)
        completed_files = [item for item in same_name_files if item["completed"]]
        self.assertEqual(len(completed_files), 1)
        self.assertTrue(completed_files[0]["source_key"].startswith("import:"))

        report = self.client.get(
            "/api/v1/reports/kiz/source-file",
            params={"source_file": "same-name.xlsx", "source_key": completed_files[0]["source_key"]},
        )
        self.assertEqual(report.status_code, 200)
        workbook = openpyxl.load_workbook(BytesIO(report.content), data_only=True)
        summary = workbook["Сводка"]
        self.assertEqual(summary["C2"].value, "Done Client")
        self.assertIsNone(summary["C3"].value)
        workbook.close()

    def test_day_report_rejects_invalid_report_date(self):
        response = self.client.get("/api/v1/reports/day?report_date=not-a-date")

        self.assertEqual(response.status_code, 422)
        self.assertIn("Invalid report_date", response.json()["detail"])

    def test_diagnostics_logs_include_failed_events_import_errors_and_redact_secrets(self):
        with self.SessionLocal() as db:
            db.add(PendingEvent(
                event_type="telegram_excel_import",
                status="failed",
                attempts=2,
                payload={},
                last_error="Bearer secret-service-token failed",
            ))
            db.add(ImportJob(
                source="telegram",
                status="failed",
                rows_total=2,
                rows_imported=0,
                raw_payload={
                    "filename": "broken.xlsx",
                    "errors": ["row 1: missing client"],
                    "invalid_rows": 1,
                    "duplicate_rows": 0,
                },
            ))
            db.add(AuditLog(
                action="skladbot_worker_sync",
                entity_type="skladbot",
                entity_id="worker",
                payload={"matched": 0, "not_found": 1, "token": "should-hide"},
            ))
            db.add(AuditLog(
                action="scan_code_created",
                entity_type="scan_code",
                entity_id="scan",
                payload={"code": "010-secret-code"},
            ))
            db.commit()

        response = self.client.get("/api/v1/diagnostics/logs")

        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8")
        self.assertIn("Failed/Pending Events", text)
        self.assertIn("telegram_excel_import", text)
        self.assertIn("broken.xlsx", text)
        self.assertIn("row 1: missing client", text)
        self.assertIn("skladbot_worker_sync", text)
        self.assertIn("Bearer ***", text)
        self.assertIn('"token": "***"', text)
        self.assertNotIn("secret-service-token", text)
        self.assertNotIn("010-secret-code", text)


if __name__ == "__main__":
    unittest.main()
