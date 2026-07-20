import io
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.app.pagination import (
    CursorError,
    NEXT_CURSOR_HEADER,
    PAGE_LIMIT_HEADER,
    cursor_filter_hash,
    decode_cursor,
    encode_cursor,
    normalize_page_limit,
    set_pagination_headers,
)
from backend.app.telegram_clients import BackendApiClient, TelegramProcessorPorts
from taksklad import backend_client


class PaginationContractTests(unittest.TestCase):
    def test_cursor_is_opaque_deterministic_scoped_and_filter_bound(self):
        filters = {"status": "active", "tags": ["one", "two"]}
        cursor = encode_cursor("orders.active.v1", [0, "2026-07-10", "order-1"], filters=filters)

        self.assertEqual(
            decode_cursor(cursor, "orders.active.v1", filters=filters),
            (0, "2026-07-10", "order-1"),
        )
        self.assertEqual(cursor, encode_cursor(
            "orders.active.v1", [0, "2026-07-10", "order-1"],
            filters={"tags": ["one", "two"], "status": "active"},
        ))
        self.assertEqual(cursor_filter_hash(filters), cursor_filter_hash(dict(reversed(list(filters.items())))))
        with self.assertRaisesRegex(CursorError, "invalid_cursor"):
            decode_cursor(cursor, "orders.returned.v1", filters=filters)
        with self.assertRaisesRegex(CursorError, "invalid_cursor"):
            decode_cursor(cursor, "orders.active.v1", filters={"status": "completed"})
        with self.assertRaisesRegex(CursorError, "invalid_cursor"):
            decode_cursor(f"{cursor}!", "orders.active.v1", filters=filters)

    def test_limit_and_headers_are_bounded_without_changing_body(self):
        self.assertEqual(normalize_page_limit(None), 50)
        self.assertEqual(normalize_page_limit("invalid"), 50)
        self.assertEqual(normalize_page_limit(0), 1)
        self.assertEqual(normalize_page_limit(999), 200)
        response = SimpleNamespace(headers={})

        set_pagination_headers(response, next_cursor="opaque-next", limit=75)

        self.assertEqual(response.headers, {
            NEXT_CURSOR_HEADER: "opaque-next",
            PAGE_LIMIT_HEADER: "75",
        })


class DesktopPaginationClientTests(unittest.TestCase):
    @staticmethod
    def http_error(status):
        return urllib.error.HTTPError(
            "https://api.taksklad.uz/api/v1/orders/active",
            status,
            "synthetic",
            {},
            io.BytesIO(b'{"detail":"synthetic auth failure"}'),
        )

    def test_request_recovers_rejected_desktop_identity_and_retries_once(self):
        class JsonResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'[{"id":"order-1"}]'

        responses = [
            self.http_error(401),
            JsonResponse(),
        ]
        opened_headers = []

        def open_url(request, timeout):
            opened_headers.append(dict(request.headers))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            mock.patch.object(backend_client, "open_https_url", side_effect=open_url),
            mock.patch.object(
                backend_client,
                "make_backend_headers",
                side_effect=[{"Authorization": "Bearer old"}, {"Authorization": "Bearer new"}],
            ),
            mock.patch(
                "taksklad.desktop_pairing.ensure_public_desktop_identity",
                return_value=True,
            ) as recover,
        ):
            payload, _headers = backend_client.backend_request_page(
                "GET", "/api/v1/orders/active",
            )

        self.assertEqual(payload, [{"id": "order-1"}])
        recover.assert_called_once_with()
        self.assertEqual(len(opened_headers), 2)
        self.assertEqual(opened_headers[0]["Authorization"], "Bearer old")
        self.assertEqual(opened_headers[1]["Authorization"], "Bearer new")

    def test_request_does_not_loop_when_recovered_identity_is_still_rejected(self):
        with (
            mock.patch.object(
                backend_client,
                "open_https_url",
                side_effect=[self.http_error(401), self.http_error(401)],
            ) as open_url,
            mock.patch(
                "taksklad.desktop_pairing.ensure_public_desktop_identity",
                return_value=True,
            ) as recover,
        ):
            with self.assertRaises(backend_client.BackendApiError) as captured:
                backend_client.backend_request_page("GET", "/api/v1/orders/active")

        self.assertEqual(captured.exception.status_code, 401)
        self.assertEqual(open_url.call_count, 2)
        recover.assert_called_once_with()

    def test_request_does_not_rotate_identity_for_non_auth_failure(self):
        with (
            mock.patch.object(
                backend_client,
                "open_https_url",
                side_effect=self.http_error(503),
            ),
            mock.patch(
                "taksklad.desktop_pairing.ensure_public_desktop_identity",
            ) as recover,
        ):
            with self.assertRaises(backend_client.BackendApiError) as captured:
                backend_client.backend_request_page("GET", "/api/v1/orders/active")

        self.assertEqual(captured.exception.status_code, 503)
        recover.assert_not_called()

    def test_all_pages_preserves_query_and_uses_old_backend_fallback(self):
        calls = []

        def request_page(_method, path, payload=None, timeout=None):
            calls.append((path, payload, timeout))
            return [{"id": "one"}], {}

        with mock.patch.object(backend_client, "backend_request_page", side_effect=request_page):
            result = backend_client.backend_request_all_pages(
                "/api/v1/orders/active?status=active", page_limit=25,
            )

        self.assertEqual(result, [{"id": "one"}])
        query = parse_qs(urlsplit(calls[0][0]).query)
        self.assertEqual(query, {"status": ["active"], "limit": ["25"]})
        self.assertEqual(len(calls), 1)

    def test_all_pages_follows_cursor_and_fails_on_repeat_or_page_cap(self):
        pages = [
            ([{"id": "one"}], {NEXT_CURSOR_HEADER: "cursor-1"}),
            ([{"id": "two"}], {}),
        ]
        calls = []

        def request_page(_method, path, payload=None, timeout=None):
            calls.append(path)
            return pages.pop(0)

        with mock.patch.object(backend_client, "backend_request_page", side_effect=request_page):
            result = backend_client.backend_request_all_pages("/api/v1/orders/active", page_limit=1)

        self.assertEqual(result, [{"id": "one"}, {"id": "two"}])
        self.assertEqual(parse_qs(urlsplit(calls[1]).query), {"limit": ["1"], "cursor": ["cursor-1"]})

        repeated = [
            ([{"id": "one"}], {NEXT_CURSOR_HEADER: "cursor-1"}),
            ([{"id": "two"}], {NEXT_CURSOR_HEADER: "cursor-1"}),
        ]
        with (
            mock.patch.object(backend_client, "backend_request_page", side_effect=repeated),
            self.assertRaisesRegex(backend_client.BackendApiError, "repeated cursor"),
        ):
            backend_client.backend_request_all_pages("/api/v1/orders/active")

        capped = [([{"id": "one"}], {NEXT_CURSOR_HEADER: "cursor-1"})]
        with (
            mock.patch.object(backend_client, "backend_request_page", side_effect=capped),
            self.assertRaisesRegex(backend_client.BackendApiError, "page safety limit"),
        ):
            backend_client.backend_request_all_pages("/api/v1/orders/active", max_pages=1)

    def test_active_orders_uses_compatible_all_pages_helper(self):
        with mock.patch.object(
            backend_client, "backend_request_all_pages", return_value=[{"id": "order-1"}],
        ) as request:
            self.assertEqual(backend_client.fetch_active_orders(), [{"id": "order-1"}])
        request.assert_called_once_with("/api/v1/orders/active")


class TelegramPaginationClientTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload, next_cursor=""):
            self.payload = payload
            self.headers = {NEXT_CURSOR_HEADER: next_cursor} if next_cursor else {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    @staticmethod
    def fake_http(responses, calls):
        class FakeClient:
            def __init__(self, timeout=None):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return responses.pop(0)

        return SimpleNamespace(
            Client=FakeClient,
            HTTPError=httpx.HTTPError,
            HTTPStatusError=httpx.HTTPStatusError,
        )

    def test_get_all_follows_cursor_and_old_backend_without_header_is_one_page(self):
        calls = []
        responses = [
            self.FakeResponse([{"id": "one"}], "cursor-1"),
            self.FakeResponse([{"id": "two"}]),
        ]
        client = BackendApiClient(
            "http://backend", token="token", http_client_module=self.fake_http(responses, calls),
        )

        result = client.get_all("/api/v1/orders/active", {"status": "active"}, page_limit=1)

        self.assertEqual(result, [{"id": "one"}, {"id": "two"}])
        self.assertEqual(calls[0][1]["params"], {"status": "active", "limit": 1})
        self.assertEqual(calls[1][1]["params"], {"status": "active", "limit": 1, "cursor": "cursor-1"})
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer token")
        self.assertRegex(
            calls[0][1]["headers"]["X-Correlation-ID"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

        fallback_calls = []
        fallback_client = BackendApiClient(
            "http://backend",
            http_client_module=self.fake_http([self.FakeResponse([{"id": "legacy"}])], fallback_calls),
        )
        self.assertEqual(fallback_client.get_all("/api/v1/imports"), [{"id": "legacy"}])
        self.assertEqual(len(fallback_calls), 1)

    def test_get_all_fails_closed_on_repeat_and_max_pages(self):
        repeated_client = BackendApiClient(
            "http://backend",
            http_client_module=self.fake_http([
                self.FakeResponse([1], "cursor-1"),
                self.FakeResponse([2], "cursor-1"),
            ], []),
        )
        with self.assertRaisesRegex(RuntimeError, "repeated cursor"):
            repeated_client.get_all("/api/v1/orders/active")

        capped_client = BackendApiClient(
            "http://backend",
            http_client_module=self.fake_http([self.FakeResponse([1], "cursor-1")], []),
        )
        with self.assertRaisesRegex(RuntimeError, "page safety limit"):
            capped_client.get_all("/api/v1/orders/active", max_pages=1)

    def test_processor_ports_pages_known_lists_and_falls_back_for_injected_legacy_client(self):
        calls = []

        class PageAwareClient:
            def get_all(self, path, params=None):
                calls.append(("get_all", path, params))
                return [{"id": "all-pages"}]

            def get(self, path, params=None):
                calls.append(("get", path, params))
                return {"id": "single"}

        ports = TelegramProcessorPorts(backend_api_client=PageAwareClient())
        self.assertEqual(ports.backend_get("/api/v1/orders/active"), [{"id": "all-pages"}])
        self.assertEqual(ports.backend_get("/api/v1/reports/day"), {"id": "single"})

        class LegacyClient:
            def get(self, path, params=None):
                calls.append(("legacy", path, params))
                return [{"id": "legacy"}]

        legacy_ports = TelegramProcessorPorts(backend_api_client=LegacyClient())
        self.assertEqual(legacy_ports.backend_get("/api/v1/imports"), [{"id": "legacy"}])
        self.assertEqual(calls, [
            ("get_all", "/api/v1/orders/active", None),
            ("get", "/api/v1/reports/day", None),
            ("legacy", "/api/v1/imports", None),
        ])


if __name__ == "__main__":
    unittest.main()
