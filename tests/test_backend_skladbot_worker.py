import ast
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.skladbot_diagnostic import diagnose_skladbot_matches
from backend.app import skladbot_client, skladbot_request_dry_run, skladbot_return_requests, skladbot_worker
from backend.app.models import AuditLog, Base, Order, OrderItem, PendingEvent
from backend.app.skladbot_worker import (
    CandidateRequests,
    SkladBotClient,
    address_soft_match,
    active_order_unloading_dates,
    business_today,
    client_matches,
    dynamic_skladbot_lookback_days,
    export_skladbot_numbers_to_google_sheets,
    fetch_candidate_requests,
    load_skladbot_fetch_cursor,
    load_skladbot_sync_orders,
    nearest_request_diagnostics,
    order_needs_skladbot_backfill,
    order_has_skladbot_number,
    parse_date,
    parse_skladbot_api_tokens,
    product_matches,
    request_created_recently,
    request_match_diagnostics,
    request_matches_order,
    request_type_matches,
    rotate_candidate_list_items,
    request_unloading_date_matches_active_orders,
    sanitize_skladbot_error,
    try_acquire_skladbot_sync_lock,
    release_skladbot_transaction_for_external_fetch,
    update_orders_from_skladbot,
)
from backend.app.skladbot_worker_runner import run_worker_cycle, worker_interval_seconds


class BackendSkladBotWorkerTests(unittest.TestCase):
    def test_skladbot_public_compatibility_uses_explicit_client_boundary(self):
        self.assertIs(skladbot_worker.SkladBotClient, skladbot_client.SkladBotClient)
        self.assertIs(skladbot_request_dry_run.SkladBotClient, skladbot_client.SkladBotClient)
        self.assertIs(skladbot_return_requests.SkladBotClient, skladbot_client.SkladBotClient)

    def test_order_and_skladbot_modules_have_no_forbidden_back_edges(self):
        module_paths = {
            name: Path("backend/app") / f"{name}.py"
            for name in (
                "orders_service",
                "skladbot_worker",
                "skladbot_request_dry_run",
                "skladbot_return_requests",
            )
        }

        def local_imports(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            return {
                node.module.split(".", 1)[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
            }

        graph = {name: local_imports(path) & module_paths.keys() for name, path in module_paths.items()}
        self.assertNotIn("skladbot_worker", graph["skladbot_request_dry_run"])
        self.assertNotIn("skladbot_worker", graph["skladbot_return_requests"])
        self.assertNotIn("orders_service", graph["skladbot_worker"])

    def test_parse_skladbot_api_tokens_accepts_pool_and_deduplicates(self):
        tokens = parse_skladbot_api_tokens({
            "SKLADBOT_API_TOKEN": "old-token",
            "SKLADBOT_API_TOKENS": " token-1,token-2 token-3;token-1 ",
        })

        self.assertEqual(tokens, ["token-1", "token-2", "token-3"])

    def test_skladbot_google_export_skips_recent_noop_export(self):
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
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={"skladbot_request_number": "WH-R-1", "skladbot_request_id": "1"},
                )
                order.items.append(OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_blocks=1,
                    quantity_pieces=10,
                    pieces_per_block=10,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                ))
                db.add(order)
                db.add(AuditLog(
                    action="skladbot_google_sheets_export",
                    entity_type="skladbot",
                    entity_id="worker",
                    payload={"status": "queued"},
                    created_at=datetime.now(timezone.utc),
                ))
                db.commit()

                with mock.patch.dict("os.environ", {"SKLADBOT_GOOGLE_EXPORT_MIN_INTERVAL_SECONDS": "300"}, clear=False):
                    result = export_skladbot_numbers_to_google_sheets(db, [order])
                events = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalars().all()

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "recent_export_cooldown")
            self.assertEqual(events, [])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_parse_skladbot_api_tokens_supports_ten_token_pool(self):
        token_pool = ",".join(f"token-{index}" for index in range(1, 11))

        self.assertEqual(
            parse_skladbot_api_tokens({"SKLADBOT_API_TOKENS": token_pool}),
            [f"token-{index}" for index in range(1, 11)],
        )

    def test_parse_skladbot_api_tokens_falls_back_to_single_token(self):
        self.assertEqual(
            parse_skladbot_api_tokens({"SKLADBOT_API_TOKEN": "single-token"}),
            ["single-token"],
        )

    def test_parse_skladbot_api_tokens_falls_back_when_pool_is_malformed(self):
        self.assertEqual(
            parse_skladbot_api_tokens({"SKLADBOT_API_TOKEN": "single-token", "SKLADBOT_API_TOKENS": ",;  ,"}),
            ["single-token"],
        )

    def test_parse_skladbot_api_tokens_empty_dict_does_not_read_process_env(self):
        with mock.patch.dict("os.environ", {"SKLADBOT_API_TOKEN": "real-env-token"}):
            self.assertEqual(parse_skladbot_api_tokens({}), [])

    def test_sanitize_skladbot_error_masks_tokens(self):
        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": "secret-token-a,secret-token-b",
        }, clear=True):
            sanitized = sanitize_skladbot_error(
                "Authorization: Bearer secret-token-a failed, secret-token-b also failed"
            )

        self.assertNotIn("secret-token-a", sanitized)
        self.assertNotIn("secret-token-b", sanitized)
        self.assertIn("Bearer ***", sanitized)

    def test_skladbot_post_error_includes_response_body(self):
        class FakeResponse:
            status_code = 422
            headers = {}
            text = '{"detail":"Недостаточно товара на складе"}'

            def json(self):
                return {"detail": "Недостаточно товара на складе"}

            def raise_for_status(self):
                raise RuntimeError("generic 422")

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                return FakeResponse()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKEN": "token-a",
            "SKLADBOT_API_TOKENS": "",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "0",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
            with self.assertRaisesRegex(RuntimeError, "Недостаточно товара"):
                SkladBotClient().create_request({"customer_id": 6211})

    def test_skladbot_client_rotates_token_on_429_without_multiplying_retries(self):
        calls = []

        class FakeResponse:
            def __init__(self, status_code, payload=None, headers=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = headers or {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError(f"unexpected HTTP {self.status_code}")

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(headers["Authorization"])
                if len(calls) == 1:
                    return FakeResponse(429, headers={"Retry-After": "30"})
                return FakeResponse(200, payload={"ok": True})

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": "token-a,token-b,token-c",
            "SKLADBOT_API_MAX_RETRIES": "2",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "20",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient), mock.patch(
            "backend.app.skladbot_client.time.sleep"
        ) as sleep_mock:
            client = SkladBotClient()
            result = client.get("/requests")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["Bearer token-a", "Bearer token-b"])
        sleep_mock.assert_called_once_with(30.0)

    def test_skladbot_client_can_reach_tenth_token_despite_default_retry_setting(self):
        calls = []

        class FakeResponse:
            def __init__(self, status_code, payload=None, headers=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = headers or {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError(f"unexpected HTTP {self.status_code}")

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(headers["Authorization"])
                if len(calls) < 10:
                    return FakeResponse(429, headers={"Retry-After": "30"})
                return FakeResponse(200, payload={"ok": True})

        token_pool = ",".join(f"token-{index}" for index in range(1, 11))
        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": token_pool,
            "SKLADBOT_API_MAX_RETRIES": "2",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "20",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient), mock.patch(
            "backend.app.skladbot_client.time.sleep"
        ) as sleep_mock:
            client = SkladBotClient()
            result = client.get("/requests")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls[-1], "Bearer token-10")
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [30.0] * 9)

    def test_skladbot_client_throttles_successive_successful_requests(self):
        calls = []

        class FakeResponse:
            status_code = 200
            headers = {}

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                return None

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(url)
                return FakeResponse()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKEN": "token-a",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "2",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient), mock.patch(
            "backend.app.skladbot_client.time.sleep"
        ) as sleep_mock:
            client = SkladBotClient()
            self.assertEqual(client.get("/requests"), {"ok": True})
            self.assertEqual(client.get("/requests/show/1"), {"ok": True})

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleep_mock.call_args_list), 1)
        self.assertGreaterEqual(sleep_mock.call_args_list[0].args[0], 0)
        self.assertLessEqual(sleep_mock.call_args_list[0].args[0], 2.0)

    def test_skladbot_client_disables_invalid_token_and_tries_next_token(self):
        calls = []

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError(f"unexpected HTTP {self.status_code}")

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(headers["Authorization"])
                if len(calls) == 1:
                    return FakeResponse(401)
                return FakeResponse(200, payload={"ok": True})

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": "token-a,token-b",
            "SKLADBOT_API_MAX_RETRIES": "2",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "0",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
            client = SkladBotClient()
            result = client.get("/requests")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["Bearer token-a", "Bearer token-b"])
        self.assertEqual(client.disabled_token_indexes, {0})

    def test_skladbot_client_rotates_token_on_timeout(self):
        calls = []

        class FakeResponse:
            status_code = 200
            headers = {}

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                return None

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(headers["Authorization"])
                if len(calls) == 1:
                    import httpx
                    raise httpx.TimeoutException("timeout")
                return FakeResponse()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": "token-a,token-b",
            "SKLADBOT_API_MAX_RETRIES": "2",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "1",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient), mock.patch(
            "backend.app.skladbot_client.time.sleep"
        ) as sleep_mock:
            client = SkladBotClient()
            result = client.get("/requests")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["Bearer token-a", "Bearer token-b"])
        sleep_mock.assert_called_once_with(1.0)

    def test_skladbot_client_throttles_server_errors_before_retry(self):
        calls = []

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError(f"unexpected HTTP {self.status_code}")

        class FakeHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url, params=None, headers=None):
                calls.append(headers["Authorization"])
                if len(calls) == 1:
                    return FakeResponse(500)
                return FakeResponse(200, payload={"ok": True})

        with mock.patch.dict("os.environ", {
            "SKLADBOT_API_TOKENS": "token-a,token-b",
            "SKLADBOT_API_MAX_RETRIES": "2",
            "SKLADBOT_REQUEST_DELAY_SECONDS": "2",
        }, clear=True), mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient), mock.patch(
            "backend.app.skladbot_client.time.sleep"
        ) as sleep_mock:
            client = SkladBotClient()
            result = client.get("/requests")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["Bearer token-a", "Bearer token-b"])
        sleep_mock.assert_called_once_with(2.0)

    def test_request_type_matches_only_outgoing_3pl(self):
        self.assertTrue(request_type_matches("3PL отгрузка"))
        self.assertTrue(request_type_matches("Отгрузка 3PL"))
        self.assertFalse(request_type_matches("Возврат 3PL"))
        self.assertFalse(request_type_matches("Возврат 3PL отгрузка"))

    def test_address_soft_match_is_diagnostic_only(self):
        self.assertTrue(address_soft_match("Tashkent, Chilanzar 10", "Uzbekistan, Tashkent, Chilanzar 10"))
        self.assertFalse(address_soft_match("Tashkent, Chilanzar 10", "Samarkand, Registan"))

    def test_client_match_ignores_quotes_case_company_form_and_warehouse_suffix(self):
        self.assertTrue(client_matches('"TABACHNAYA LAVKA" MCHJ', '"Tabachnaya Lavka" MCHJ (склади)'))

    def test_product_match_accepts_concatenated_vendor_code(self):
        self.assertTrue(product_matches("Chapman Brown OP 20", "CHPMBrownOP20UZ"))
        self.assertTrue(product_matches("Chapman Brown SSL 100`20", "CHPMBrownSSL20UZ"))
        self.assertTrue(product_matches("Chapman Gold SSL 100`20", "CHPMGoldSSL20UZ"))
        self.assertTrue(product_matches("Chapman Green OP 20", "CHPMGreenOP20UZ"))
        self.assertTrue(product_matches("Chapman RED SSL 100 20", "CHPMRedSSL20UZ"))
        self.assertFalse(product_matches("Chapman Brown OP 20", "CHPMRedOP20UZ"))
        self.assertFalse(product_matches("Chapman Brown SSL 100`20", "CHPMBrownOP20UZ"))

    def test_request_matches_order_by_date_payment_client_products_and_blocks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Адрес может отличаться",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_pieces=200,
                quantity_blocks=20,
                pieces_per_block=10,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ (склади)',
            "comment": "ТЕРМИНАЛ",
            "address": "Другой адрес",
            "products": [
                {
                    "name": "Chapman Brown OP 20 UZ - KingSize",
                    "vendor_code": "CHPMBrownOP20UZ",
                    "barcode": "4006396053978",
                    "amount": 20,
                },
            ],
        }

        self.assertTrue(request_matches_order(order, request))

    def test_request_matches_when_skladbot_contains_extra_products(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Адрес может отличаться",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_pieces=200,
                quantity_blocks=20,
                pieces_per_block=10,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ (склади)',
            "comment": "Терминал",
            "products": [
                {"name": "Chapman Brown OP 20 UZ", "vendor_code": "CHPMBrownOP20UZ", "amount": 20},
                {"name": "Chapman Gold SSL 20 UZ", "vendor_code": "CHPMGoldSSL20UZ", "amount": 3},
            ],
        }

        diagnostic = request_match_diagnostics(order, request)

        self.assertTrue(diagnostic["matched"])
        self.assertTrue(diagnostic["checks"]["products"])
        self.assertFalse(diagnostic["address_soft_match"])
        self.assertEqual(diagnostic["extra_request_products"], 1)

    def test_request_without_order_products_does_not_match(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = []
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ',
            "comment": "Терминал",
            "products": [
                {"name": "Chapman Brown OP 20 UZ", "amount": 20},
            ],
        }

        self.assertFalse(request_matches_order(order, request))

    def test_request_match_diagnostics_explains_failed_checks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_blocks=20,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "30.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ',
            "comment": "Терминал",
            "products": [
                {
                    "name": "Chapman Brown OP 20 UZ",
                    "amount": 20,
                },
            ],
        }

        diagnostic = request_match_diagnostics(order, request)

        self.assertFalse(diagnostic["matched"])
        self.assertFalse(diagnostic["checks"]["date"])
        self.assertTrue(diagnostic["checks"]["client"])
        self.assertTrue(diagnostic["checks"]["payment"])
        self.assertTrue(diagnostic["checks"]["products"])

    def test_nearest_request_diagnostics_lists_failed_checks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_blocks=2,
                status="not_completed",
                raw_payload={},
            ),
        ]
        requests = [
            {
                "id": 1,
                "number": "WH-R-1",
                "unloading_date": "29.05.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 1}],
            }
        ]

        nearest = nearest_request_diagnostics(order, requests)

        self.assertEqual(nearest[0]["number"], "WH-R-1")
        self.assertEqual(nearest[0]["failed_checks"], ["products"])
        self.assertFalse(nearest[0]["products"][0]["matched"])

    def test_update_orders_exports_skladbot_numbers_to_google_sheets(self):
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
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        quantity_pieces=20,
                        pieces_per_block=10,
                        scanned_blocks=0,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                db.add(order)
                db.commit()

            request = {
                "id": 191794,
                "number": "WH-R-191794",
                "unloading_date": "29.05.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 2}],
                "raw": {},
            }
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=CandidateRequests([request], complete=True),
            ) as fetch_candidates:
                result = update_orders_from_skladbot()

            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["active_orders"], 1)
            self.assertEqual(result["completed_backfill_orders"], 0)
            self.assertEqual(result["google_sheets_export"]["status"], "queued")
            self.assertEqual(len(fetch_candidates.call_args.kwargs["orders"]), 1)
            with SessionLocal() as db:
                order = db.execute(select(Order)).scalar_one()
                self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-191794")
                self.assertEqual(order.raw_payload["skladbot_request_id"], "191794")
                audit = db.execute(
                    select(AuditLog).where(AuditLog.action == "skladbot_google_sheets_export")
                ).scalar_one()
                self.assertEqual(audit.payload["status"], "queued")
                event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
                self.assertEqual(event.payload["action"], "google_sheets_skladbot_export")
                self.assertFalse(event.payload["include_inactive"])
                self.assertFalse(event.payload["include_archive"])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_update_orders_backfills_fresh_completed_order_without_skladbot_number(self):
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
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="completed",
                    raw_payload={},
                )
                order.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        quantity_pieces=20,
                        pieces_per_block=10,
                        scanned_blocks=2,
                        status="completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                db.add(order)
                db.commit()

            request = {
                "id": 191794,
                "number": "WH-R-191794",
                "unloading_date": "05.06.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 2}],
                "raw": {},
            }
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=CandidateRequests([request], complete=True),
            ) as fetch_candidates:
                result = update_orders_from_skladbot()

            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["active_orders"], 0)
            self.assertEqual(result["completed_backfill_orders"], 1)
            self.assertEqual(len(fetch_candidates.call_args.kwargs["orders"]), 1)
            with SessionLocal() as db:
                order = db.execute(select(Order)).scalar_one()
                self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-191794")
                self.assertEqual(order.raw_payload["skladbot_request_id"], "191794")
                self.assertEqual(order.raw_payload["skladbot_status"], "found")
                event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
                self.assertEqual(event.payload["action"], "google_sheets_skladbot_export")
                self.assertTrue(event.payload["include_inactive"])
                self.assertTrue(event.payload["include_archive"])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_load_skladbot_sync_orders_skips_stale_completed_backfill_orders(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            old_time = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
            with SessionLocal() as db:
                fresh = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Fresh",
                    address="Address",
                    status="completed",
                    raw_payload={},
                    updated_at=old_time,
                )
                fresh.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1, status="completed")]
                stale = Order(
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client="Stale",
                    address="Address",
                    status="completed",
                    raw_payload={},
                    updated_at=old_time,
                )
                stale.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1, status="completed")]
                returned = Order(
                    order_date=date(2026, 6, 5),
                    payment_type="Перечисление",
                    client="Returned",
                    address="Address",
                    status="returned",
                    raw_payload={},
                )
                returned.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1, status="returned")]
                db.add_all([fresh, stale, returned])
                db.commit()

            with SessionLocal() as db, mock.patch(
                "backend.app.skladbot_worker.completed_backfill_cutoffs",
                return_value=(date(2026, 6, 3), datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)),
            ):
                orders, active_orders, completed_backfill_orders = load_skladbot_sync_orders(db)

            self.assertEqual(active_orders, [])
            self.assertEqual([order.client for order in completed_backfill_orders], ["Fresh"])
            self.assertEqual([order.client for order in orders], ["Fresh"])
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_order_needs_skladbot_backfill_requires_number_and_id(self):
        self.assertTrue(order_needs_skladbot_backfill(Order(raw_payload={})))
        self.assertTrue(order_needs_skladbot_backfill(Order(raw_payload={"skladbot_request_id": "191794"})))
        self.assertTrue(order_needs_skladbot_backfill(Order(raw_payload={"skladbot_request_number": "WH-R-191794"})))
        self.assertFalse(order_needs_skladbot_backfill(Order(raw_payload={
            "skladbot_request_number": "WH-R-191794",
            "skladbot_request_id": "191794",
        })))

    def test_update_orders_exports_all_active_orders_to_google_sheets_after_match(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                existing = Order(
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"EXISTING CLIENT" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={
                        "skladbot_request_number": "WH-R-100",
                        "skladbot_request_id": "100",
                        "skladbot_status": "found",
                    },
                )
                existing.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=1,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                missing = Order(
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={},
                )
                missing.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-2", "source_order_id": "order-2"},
                    )
                ]
                db.add_all([existing, missing])
                db.commit()

            request = {
                "id": 191794,
                "number": "WH-R-191794",
                "unloading_date": "29.05.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 2}],
                "raw": {},
            }
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=CandidateRequests([request], complete=True),
            ):
                result = update_orders_from_skladbot()

            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["google_sheets_export"]["status"], "queued")
            with SessionLocal() as db:
                event = db.execute(select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")).scalar_one()
                self.assertEqual(len(event.payload["order_ids"]), 2)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_update_orders_reexports_existing_skladbot_numbers_to_google_sheets(self):
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
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={
                        "skladbot_request_number": "WH-R-191794",
                        "skladbot_request_id": "191794",
                        "skladbot_status": "found",
                    },
                )
                order.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        quantity_pieces=20,
                        pieces_per_block=10,
                        scanned_blocks=0,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                db.add(order)
                db.commit()

            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
            ) as fetch_candidates:
                result = update_orders_from_skladbot()

            self.assertEqual(result["already_numbered"], 1)
            self.assertEqual(result["google_sheets_export"]["status"], "queued")
            fetch_candidates.assert_not_called()
            with SessionLocal() as db:
                audit = db.execute(
                    select(AuditLog).where(AuditLog.action == "skladbot_google_sheets_export")
                ).scalar_one()
                self.assertEqual(audit.payload["status"], "queued")
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_update_orders_marks_pending_when_skladbot_check_is_incomplete(self):
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
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                db.add(order)
                db.commit()

            incomplete_requests = CandidateRequests(
                [],
                complete=False,
                reason="detail_limit_reached",
                details_checked=1,
                detail_limit=1,
            )
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=incomplete_requests,
            ):
                result = update_orders_from_skladbot()

            self.assertEqual(result["not_found"], 0)
            self.assertEqual(result["incomplete"], 1)
            self.assertEqual(result["pending"], 1)
            with SessionLocal() as db:
                order = db.execute(select(Order)).scalar_one()
                self.assertEqual(order.raw_payload["skladbot_status"], "pending")
                self.assertNotIn("skladbot_error", order.raw_payload)
                self.assertEqual(order.raw_payload["skladbot_fetch"]["reason"], "detail_limit_reached")
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_incomplete_fetch_does_not_overwrite_create_queue_state(self):
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
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TEST CLIENT" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={"skladbot_status": "create_queued"},
                )
                order.items = [OrderItem(
                    product="Chapman Brown OP 20",
                    quantity_blocks=2,
                    status="not_completed",
                    raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                )]
                db.add(order)
                db.commit()

            incomplete_requests = CandidateRequests([], complete=False, reason="detail_limit_reached")
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=incomplete_requests,
            ):
                result = update_orders_from_skladbot()

            self.assertEqual(result["pending"], 1)
            with SessionLocal() as db:
                order = db.execute(select(Order)).scalar_one()
                self.assertEqual(order.raw_payload["skladbot_status"], "create_queued")
                self.assertEqual(order.raw_payload["skladbot_fetch"]["reason"], "detail_limit_reached")
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_load_skladbot_fetch_cursor_reads_last_checked_request_id(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                db.add(AuditLog(
                    action="skladbot_worker_sync",
                    entity_type="skladbot",
                    entity_id="worker",
                    payload={"fetch": {"last_checked_request_id": 192991}},
                ))
                db.commit()

            with SessionLocal() as db:
                self.assertEqual(load_skladbot_fetch_cursor(db), 192991)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_rotate_candidate_list_items_continues_after_previous_request(self):
        items = [
            (0, date(2026, 6, 4), 193004, {"id": 193004}),
            (0, date(2026, 6, 4), 193003, {"id": 193003}),
            (0, date(2026, 6, 4), 193002, {"id": 193002}),
            (0, date(2026, 6, 4), 192991, {"id": 192991}),
        ]

        rotated = rotate_candidate_list_items(items, start_after_request_id=193002)

        self.assertEqual([item[2] for item in rotated], [192991, 193004, 193003, 193002])

    def test_fetch_candidates_uses_cursor_with_default_detail_limit(self):
        class FakeClient:
            configured = True
            request_delay = 0
            limit = 100

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {
                        "id": request_id,
                        "type": "Отгрузка 3PL",
                        "created_at": "2026-06-03",
                        "updated_at": "2026-06-03",
                        "unloading_date": "04.06.2026",
                        "delivery_number": f"WH-R-{request_id}",
                    }
                    for request_id in (193004, 193003, 193002, 192991)
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-06-03",
                    "comment": "Терминал",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "04.06.2026"},
                        {"name": "Название компании/Имя человека", "value": '"OTHER CLIENT" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 1},
                    ],
                }

        order = Order(
            order_date=date(2026, 6, 4),
            payment_type="Терминал",
            client='"TARGET CLIENT" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1)]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {"SKLADBOT_DETAIL_LIMIT": "3"}, clear=True):
            result = fetch_candidate_requests(
                today=date(2026, 6, 3),
                orders=[order],
                client=fake_client,
                start_after_request_id=193002,
            )

        self.assertEqual(fake_client.detail_ids, [192991, 193004, 193003])
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "detail_limit_reached")
        self.assertEqual(result.last_checked_request_id, 193003)
        self.assertEqual(result.checked_request_ids, [192991, 193004, 193003])

    def test_candidate_window_uses_created_date_not_future_unloading_date(self):
        request = {
            "created_at": "2026-05-31 10:00:00",
            "updated_at": "",
            "unloading_date": "02.06.2026",
        }

        self.assertTrue(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_business_today_uses_configured_sklad_timezone(self):
        with mock.patch.dict("os.environ", {"TAKSKLAD_TIMEZONE": "Asia/Tashkent"}):
            self.assertEqual(
                business_today(datetime(2026, 5, 31, 20, 30, tzinfo=timezone.utc)),
                date(2026, 6, 1),
            )

    def test_skladbot_timestamp_dates_use_business_timezone(self):
        with mock.patch.dict("os.environ", {"TAKSKLAD_TIMEZONE": "Asia/Tashkent"}):
            self.assertEqual(parse_date("2026-05-31T20:30:00+00:00"), date(2026, 6, 1))
            self.assertEqual(parse_date("2026-05-31 20:30:00+00:00"), date(2026, 6, 1))
            self.assertEqual(parse_date("31.05.2026 20:30:00+0000"), date(2026, 6, 1))
            self.assertEqual(parse_date("31.05.2026 20:30:00"), date(2026, 5, 31))
            self.assertTrue(
                request_created_recently(
                    {"created_at": "31.05.2026 20:30:00+0000", "updated_at": ""},
                    today=date(2026, 6, 1),
                    lookback_days=0,
                )
            )

    def test_candidate_window_rejects_old_created_request(self):
        request = {
            "created_at": "2026-05-20",
            "updated_at": "",
            "unloading_date": "31.05.2026",
        }

        self.assertFalse(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_candidate_window_rejects_request_without_created_or_updated_date(self):
        request = {
            "created_at": "",
            "updated_at": "",
            "unloading_date": "31.05.2026",
        }

        self.assertFalse(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_fetch_candidates_skips_old_list_items_before_detail_fetch(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-20", "delivery_number": "OLD"},
                    {"id": 2, "type": "Отгрузка 3PL", "created_at": "2026-05-31", "delivery_number": "NEW"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "type": "Отгрузка 3PL",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": "Client"},
                    ],
                }

        fake_client = FakeClient()
        with mock.patch("backend.app.skladbot_worker.SkladBotClient", return_value=fake_client):
            result = fetch_candidate_requests(today=date(2026, 5, 31))

        self.assertEqual(fake_client.detail_ids, [2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], "NEW")

    def test_dynamic_lookback_expands_for_older_active_orders_with_cap(self):
        orders = [
            Order(order_date=date(2026, 5, 29), raw_payload={}),
        ]

        with mock.patch.dict("os.environ", {
            "SKLADBOT_SYNC_LOOKBACK_DAYS": "1",
            "SKLADBOT_SYNC_MAX_LOOKBACK_DAYS": "7",
            "SKLADBOT_ORDER_CREATE_LEAD_DAYS": "3",
        }):
            self.assertEqual(
                dynamic_skladbot_lookback_days(
                    orders=orders,
                    today=date(2026, 6, 1),
                ),
                6,
            )

    def test_fetch_candidates_uses_dynamic_lookback_for_active_order_date(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-26", "delivery_number": "WH-R-1"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-1",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-26",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": '"TABACHNAYA LAVKA" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 2},
                    ],
                }

        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(product="Chapman Brown OP 20", quantity_blocks=2),
        ]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_SYNC_LOOKBACK_DAYS": "1",
            "SKLADBOT_SYNC_MAX_LOOKBACK_DAYS": "7",
            "SKLADBOT_DETAIL_LIMIT": "30",
        }):
            result = fetch_candidate_requests(today=date(2026, 6, 1), orders=[order], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], "WH-R-1")

    def test_candidate_window_includes_tomorrow_and_active_order_dates(self):
        orders = [
            Order(order_date=date(2026, 6, 5), raw_payload={}),
        ]

        self.assertEqual(
            active_order_unloading_dates(orders=orders, today=date(2026, 6, 1)),
            {date(2026, 6, 2), date(2026, 6, 5)},
        )
        self.assertTrue(
            request_unloading_date_matches_active_orders(
                {"unloading_date": "05.06.2026"},
                orders=orders,
                today=date(2026, 6, 1),
            )
        )
        self.assertTrue(
            request_unloading_date_matches_active_orders(
                {"unloading_date": "02.06.2026"},
                orders=[],
                today=date(2026, 6, 1),
            )
        )

    def test_fetch_candidates_keeps_old_created_request_when_unloading_date_matches_active_order(self):
        class FakeClient:
            configured = True
            request_delay = 0
            limit = 500

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-20", "delivery_number": "WH-R-1"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-1",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-20",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "05.06.2026"},
                        {"name": "Название компании/Имя человека", "value": '"TABACHNAYA LAVKA" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 2},
                    ],
                }

        order = Order(
            order_date=date(2026, 6, 5),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(product="Chapman Brown OP 20", quantity_blocks=2),
        ]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_SYNC_LOOKBACK_DAYS": "1",
            "SKLADBOT_SYNC_MAX_LOOKBACK_DAYS": "7",
            "SKLADBOT_DETAIL_LIMIT": "30",
        }):
            result = fetch_candidate_requests(today=date(2026, 6, 1), orders=[order], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], "WH-R-1")
        self.assertTrue(result.complete)

    def test_fetch_candidates_default_detail_limit_is_10_and_prioritizes_fresh_requests(self):
        class FakeClient:
            configured = True
            request_delay = 0
            limit = 500

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                old_items = [
                    {
                        "id": request_id,
                        "type": "Отгрузка 3PL",
                        "created_at": "2026-05-20",
                        "updated_at": "2026-05-20",
                        "unloading_date": "05.06.2026",
                        "delivery_number": f"WH-R-{request_id}",
                    }
                    for request_id in range(1, 31)
                ]
                return [
                    *old_items,
                    {
                        "id": 99,
                        "type": "Отгрузка 3PL",
                        "created_at": "2026-06-01",
                        "updated_at": "2026-06-01",
                        "unloading_date": "05.06.2026",
                        "delivery_number": "WH-R-99",
                    },
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-06-01" if request_id == 99 else "2026-05-20",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "05.06.2026"},
                        {"name": "Название компании/Имя человека", "value": '"OTHER CLIENT" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 2},
                    ],
                }

        order = Order(
            order_date=date(2026, 6, 5),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(product="Chapman Brown OP 20", quantity_blocks=2),
        ]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {}, clear=True):
            result = fetch_candidate_requests(today=date(2026, 6, 1), orders=[order], client=fake_client)

        self.assertEqual(len(fake_client.detail_ids), 10)
        self.assertEqual(fake_client.detail_ids[0], 99)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "detail_limit_reached")

    def test_fetch_candidates_marks_result_incomplete_when_detail_limit_blocks_full_check(self):
        class FakeClient:
            configured = True
            request_delay = 0
            limit = 500

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-20", "delivery_number": "WH-R-1"},
                    {"id": 2, "type": "Отгрузка 3PL", "created_at": "2026-05-20", "delivery_number": "WH-R-2"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-20",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "05.06.2026"},
                        {"name": "Название компании/Имя человека", "value": '"OTHER CLIENT" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 2},
                    ],
                }

        order = Order(
            order_date=date(2026, 6, 5),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(product="Chapman Brown OP 20", quantity_blocks=2),
        ]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {"SKLADBOT_DETAIL_LIMIT": "1"}):
            result = fetch_candidate_requests(today=date(2026, 6, 1), orders=[order], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1])
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "detail_limit_reached")

    def test_diagnostic_uses_active_orders_for_candidate_window(self):
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
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=2)]
                db.add(order)
                db.commit()

            with mock.patch("backend.app.skladbot_diagnostic.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_diagnostic.fetch_candidate_requests",
                return_value=[],
            ) as fetch_candidates:
                result = diagnose_skladbot_matches(limit=10, request_limit=20)

            self.assertEqual(result["active_orders"], 1)
            fetch_candidates.assert_called_once()
            self.assertEqual(len(fetch_candidates.call_args.kwargs["orders"]), 1)
            self.assertEqual(fetch_candidates.call_args.kwargs["orders"][0].order_date, date(2026, 5, 29))
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_fetch_candidates_stops_after_all_active_orders_matched(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-1"},
                    {"id": 2, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-2"},
                    {"id": 3, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-3"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                recipient = '"FIRST CLIENT" MCHJ' if request_id == 1 else '"SECOND CLIENT" MCHJ'
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-29",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": recipient},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 1},
                    ],
                }

        first = Order(order_date=date(2026, 5, 29), payment_type="Перечисление", client='"FIRST CLIENT" MCHJ', raw_payload={})
        second = Order(order_date=date(2026, 5, 29), payment_type="Перечисление", client='"SECOND CLIENT" MCHJ', raw_payload={})
        first.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1)]
        second.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1)]
        fake_client = FakeClient()

        result = fetch_candidate_requests(today=date(2026, 5, 29), orders=[first, second], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1, 2])
        self.assertEqual([item["number"] for item in result], ["WH-R-1", "WH-R-2"])

    def test_order_has_skladbot_number_accepts_number_or_id(self):
        self.assertFalse(order_has_skladbot_number(Order(raw_payload={})))
        self.assertTrue(order_has_skladbot_number(Order(raw_payload={"skladbot_request_number": "WH-R-1"})))
        self.assertTrue(order_has_skladbot_number(Order(raw_payload={"skladbot_request_id": "191794"})))

    def test_worker_default_interval_is_fast_but_not_below_one_minute(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "10"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "120"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 120)

    def test_worker_runner_routes_each_processor_once(self):
        db = object()
        context = mock.MagicMock()
        context.__enter__.return_value = db
        create_processor = mock.Mock(return_value={"checked": 1})
        return_processor = mock.Mock(return_value={"checked": 2})
        sync_processor = mock.Mock(return_value={"updated": 3})

        result = run_worker_cycle(
            session_factory=lambda: context,
            create_processor=create_processor,
            return_processor=return_processor,
            sync_processor=sync_processor,
        )

        create_processor.assert_called_once_with(db)
        return_processor.assert_called_once_with(db)
        sync_processor.assert_called_once_with()
        self.assertEqual(result, {
            "create": {"checked": 1},
            "return": {"checked": 2},
            "sync": {"updated": 3},
        })

    def test_external_fetch_boundary_releases_database_transaction(self):
        db = mock.Mock()

        release_skladbot_transaction_for_external_fetch(db)

        self.assertEqual(db.method_calls, [mock.call.expunge_all(), mock.call.commit()])

    def test_legacy_worker_entrypoint_delegates_once_to_runner(self):
        runner = mock.Mock()
        runner.main.return_value = "completed"

        with mock.patch.object(skladbot_worker, "_load_worker_runner", return_value=runner) as load_runner:
            result = skladbot_worker.main()

        load_runner.assert_called_once_with()
        runner.main.assert_called_once_with()
        self.assertEqual(result, "completed")

    def test_legacy_worker_interval_delegates_once_to_runner(self):
        runner = mock.Mock()
        runner.worker_interval_seconds.return_value = 120

        with mock.patch.object(skladbot_worker, "_load_worker_runner", return_value=runner) as load_runner:
            result = skladbot_worker.worker_interval_seconds()

        load_runner.assert_called_once_with()
        runner.worker_interval_seconds.assert_called_once_with()
        self.assertEqual(result, 120)

    def test_postgres_skladbot_lock_is_transaction_scoped(self):
        db = mock.Mock()
        db.bind.dialect.name = "postgresql"
        db.execute.return_value.scalar.return_value = True

        self.assertTrue(try_acquire_skladbot_sync_lock(db))

        sql = str(db.execute.call_args.args[0])
        self.assertIn("pg_try_advisory_xact_lock", sql)
        self.assertNotIn("pg_try_advisory_lock(", sql)


if __name__ == "__main__":
    unittest.main()
