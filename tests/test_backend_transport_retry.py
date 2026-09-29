import http.client
import io
import json
import socket
import ssl
import unittest
import urllib.error
from unittest import mock

from taksklad import backend_client, http_client
from taksklad.backend_client import BackendApiError, BackendTransportError
from taksklad.backend_events import backend_error_kind
from tests.test_backend_keepalive import FakeConnection


class JsonResponse:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok":true}'


def handshake_timeout():
    # Ошибка снята с боевого экрана склада 09.09.2026.
    return urllib.error.URLError("_ssl.c:993: The handshake operation timed out")


def failed_before_send():
    # Соединение не установилось: сервер запроса не видел.
    return http_client.mark_failed_before_send(handshake_timeout())


def failed_reading_answer():
    # Запрос ушёл целиком, сервер мог его выполнить.
    return http.client.RemoteDisconnected("Remote end closed connection without response")


# Изменяющие запросы, повтор которых после отправки может дать чужой отказ.
NON_REPLAYABLE_REQUESTS = (
    ("POST", "/api/v1/scans/undo"),
    ("POST", "/api/v1/kiz/release"),
    ("POST", "/api/v1/returns/order-1"),
    ("POST", "/api/v1/something/new"),
)
# Запросы, которые сервер выполняет идемпотентно.
REPLAYABLE_REQUESTS = (
    ("GET", "/api/v1/orders/active"),
    ("POST", "/api/v1/scans"),
    ("POST", "/api/v1/orders/order-1/complete"),
    ("POST", "/api/v1/imports"),
    ("POST", "/api/v1/imports/preview"),
)


class BackendTransportRetryTests(unittest.TestCase):
    @staticmethod
    def http_error(status, detail="synthetic"):
        return urllib.error.HTTPError(
            "https://api.taksklad.uz/api/v1/scans",
            status,
            "synthetic",
            {},
            io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")),
        )

    @staticmethod
    def run_request(method, path, outcomes):
        """Прогон запроса по сценарию: исключение или ответ на каждую попытку."""
        calls = []

        def open_url(request, timeout):
            calls.append(timeout)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep"),
        ):
            try:
                result, _headers = backend_client.backend_request_page(method, path, payload={})
            except BackendApiError as exc:
                return exc, len(calls)
        return result, len(calls)

    def test_dropped_handshake_is_retried_on_a_fresh_attempt(self):
        attempts = []

        def open_url(request, timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise handshake_timeout()
            return JsonResponse()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep") as sleep,
        ):
            payload, _headers = backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(attempts), 2)
        sleep.assert_called_once()

    def test_retry_waits_longer_than_the_first_attempt(self):
        attempts = []

        def open_url(request, timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise handshake_timeout()
            return JsonResponse()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep"),
        ):
            backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertGreater(attempts[1], attempts[0])

    def test_transport_failure_stops_after_the_last_attempt(self):
        def open_url(request, timeout):
            raise handshake_timeout()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url) as opened,
            mock.patch.object(backend_client.time, "sleep"),
        ):
            with self.assertRaises(BackendApiError) as raised:
                backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertEqual(opened.call_count, 2)
        self.assertIn("handshake", str(raised.exception))
        self.assertIsNone(raised.exception.status_code)

    def test_stale_keepalive_connection_is_retried_on_a_fresh_one(self):
        # Сервер закрывает простаивающее соединение молча: первый запрос после
        # паузы падает не как отказ, а как обрыв, и обязан пойти повторно.
        attempts = []

        def open_url(request, timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise http.client.BadStatusLine("")
            return JsonResponse()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep"),
        ):
            payload, _headers = backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(attempts), 2)

    def test_server_rejection_is_not_retried(self):
        with (
            mock.patch.object(
                backend_client,
                "open_backend_https_url",
                side_effect=self.http_error(400),
            ) as opened,
            mock.patch.object(backend_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(BackendApiError):
                backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()

    def test_transport_failure_is_raised_as_transport_error_of_network_kind(self):
        causes = [
            handshake_timeout(),
            ssl.SSLError("handshake failure"),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
            http.client.RemoteDisconnected("closed"),
            socket.gaierror(11001, "getaddrinfo failed"),
        ]
        for cause in causes:
            with self.subTest(cause=type(cause).__name__):
                with (
                    mock.patch.object(backend_client, "open_backend_https_url", side_effect=cause),
                    mock.patch.object(backend_client.time, "sleep"),
                ):
                    with self.assertRaises(BackendApiError) as raised:
                        backend_client.backend_request_page("GET", "/api/v1/orders/active")

                self.assertIsInstance(raised.exception, BackendTransportError)
                self.assertIs(raised.exception.__cause__, cause)
                self.assertEqual(backend_error_kind(raised.exception), "network")

    def test_unreadable_answer_without_status_is_not_a_network_error(self):
        # 200 от прокси с HTML вместо JSON: ответ пришёл, канал жив.
        class HtmlResponse(JsonResponse):
            def read(self):
                return b"<html>proxy login</html>"

        with mock.patch.object(backend_client, "open_backend_https_url", return_value=HtmlResponse()):
            with self.assertRaises(BackendApiError) as raised:
                backend_client.backend_request_page("GET", "/api/v1/orders/active")

        self.assertNotIsInstance(raised.exception, BackendTransportError)
        self.assertIsNone(raised.exception.status_code)
        self.assertEqual(backend_error_kind(raised.exception), "server")

    def test_client_bug_without_status_is_not_a_network_error(self):
        with mock.patch.object(backend_client, "open_backend_https_url", side_effect=KeyError("boom")):
            with self.assertRaises(BackendApiError) as raised:
                backend_client.backend_request_page("GET", "/api/v1/orders/active")

        self.assertNotIsInstance(raised.exception, BackendTransportError)
        self.assertEqual(backend_error_kind(raised.exception), "server")

    def test_replayable_requests_are_retried_after_a_failure_reading_the_answer(self):
        for method, path in REPLAYABLE_REQUESTS:
            with self.subTest(method=method, path=path):
                result, attempts = self.run_request(method, path, [failed_reading_answer(), JsonResponse()])

                self.assertEqual(result, {"ok": True})
                self.assertEqual(attempts, 2)

    def test_non_replayable_requests_are_not_retried_after_a_failure_reading_the_answer(self):
        # Сервер мог уже выполнить запрос: повтор получил бы 404 на undo,
        # 409 на возврат и released=False на освобождение КИЗ.
        for method, path in NON_REPLAYABLE_REQUESTS:
            with self.subTest(method=method, path=path):
                result, attempts = self.run_request(method, path, [failed_reading_answer(), JsonResponse()])

                self.assertIsInstance(result, BackendTransportError)
                self.assertEqual(attempts, 1)

    def test_non_replayable_requests_are_retried_after_a_failure_before_send(self):
        for method, path in NON_REPLAYABLE_REQUESTS:
            with self.subTest(method=method, path=path):
                result, attempts = self.run_request(method, path, [failed_before_send(), JsonResponse()])

                self.assertEqual(result, {"ok": True})
                self.assertEqual(attempts, 2)

    def test_non_replayable_request_with_unknown_failure_phase_is_not_retried(self):
        result, attempts = self.run_request("POST", "/api/v1/scans/undo", [handshake_timeout(), JsonResponse()])

        self.assertIsInstance(result, BackendTransportError)
        self.assertEqual(attempts, 1)

    def test_replay_safety_table(self):
        safe = REPLAYABLE_REQUESTS + (
            ("POST", "/api/v1/sync/sources?skladbot=1&wait_skladbot=0"),
            ("GET", "/api/v1/returns/lookup?lookup=abc"),
        )
        for method, path in safe:
            with self.subTest(method=method, path=path):
                self.assertTrue(backend_client.request_is_replay_safe(method, path))
        for method, path in NON_REPLAYABLE_REQUESTS + (("PUT", "/api/v1/scans"), ("DELETE", "/api/v1/scans")):
            with self.subTest(method=method, path=path):
                self.assertFalse(backend_client.request_is_replay_safe(method, path))

    def test_undo_not_found_right_after_a_transport_error_is_a_successful_undo(self):
        # Сервер снял код, а ответ потерялся: повтор получает 404 на уже
        # снятый код, и отмена не должна возвращать код в список.
        result, attempts = self.run_request(
            "POST",
            "/api/v1/scans/undo",
            [failed_before_send(), self.http_error(404, "Scan code was not found for this order item")],
        )

        self.assertEqual(result, {"status": "already_undone"})
        self.assertEqual(attempts, 2)

    def test_undo_not_found_without_a_transport_error_stays_an_error(self):
        result, attempts = self.run_request(
            "POST",
            "/api/v1/scans/undo",
            [self.http_error(404, "Scan code was not found for this order item")],
        )

        self.assertIsInstance(result, BackendApiError)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(attempts, 1)

    def test_other_not_found_after_a_transport_error_stays_an_error(self):
        result, attempts = self.run_request(
            "POST",
            "/api/v1/scans/undo",
            [failed_before_send(), self.http_error(404, "Order item was not found")],
        )

        self.assertIsInstance(result, BackendApiError)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(attempts, 2)

    def test_scan_not_found_after_a_transport_error_is_not_swallowed_for_other_requests(self):
        result, attempts = self.run_request(
            "POST",
            "/api/v1/returns/order-1",
            [failed_before_send(), self.http_error(404, "Scan code was not found for this order item")],
        )

        self.assertIsInstance(result, BackendApiError)
        self.assertEqual(result.status_code, 404)

    def test_retry_budget_is_doubled_but_capped_at_twenty_seconds(self):
        # Бюджет повтора min(base * 2, 20): при 8 секундах прежние 16, а большой
        # таймаут не разгоняется до минуты и более.
        for base, expected_retry in ((8, 16), (10, 20), (15, 20)):
            with self.subTest(base=base):
                timeouts = []

                def open_url(request, timeout):
                    timeouts.append(timeout)
                    if len(timeouts) == 1:
                        raise handshake_timeout()
                    return JsonResponse()

                with (
                    mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
                    mock.patch.object(backend_client.time, "sleep"),
                ):
                    backend_client.backend_request("GET", "/api/v1/orders/active", timeout=base)

                self.assertEqual(timeouts, [base, expected_retry])

    def test_default_timeout_setting_gives_the_same_capped_retry_budget(self):
        timeouts = []

        def open_url(request, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise handshake_timeout()
            return JsonResponse()

        with (
            mock.patch.object(backend_client, "TAKSKLAD_BACKEND_TIMEOUT_SECONDS", 5),
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep"),
        ):
            backend_client.backend_request("GET", "/api/v1/orders/active")

        self.assertEqual(timeouts, [5, 10])

    def test_kiz_availability_lookup_with_its_short_timeout_is_not_retried(self):
        # Проверка идёт на главном потоке Tk с намеренными 3 секундами: повтор
        # растянул бы её до 9,5 секунды.
        timeouts = []

        def open_url(request, timeout):
            timeouts.append(timeout)
            raise handshake_timeout()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(BackendTransportError):
                backend_client.lookup_kiz_availability("CODE", order_item_id="item-1")

        self.assertEqual(timeouts, [3])
        sleep.assert_not_called()

    def test_explicit_timeout_below_eight_seconds_is_not_retried(self):
        for timeout in (1, 3, 7):
            with self.subTest(timeout=timeout):
                attempts = []

                def open_url(request, timeout, attempts=attempts):
                    attempts.append(timeout)
                    raise handshake_timeout()

                with (
                    mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
                    mock.patch.object(backend_client.time, "sleep"),
                ):
                    with self.assertRaises(BackendTransportError):
                        backend_client.backend_request("GET", "/api/v1/orders/active", timeout=timeout)

                self.assertEqual(len(attempts), 1)

    def test_explicit_timeout_of_eight_seconds_is_still_retried(self):
        with (
            mock.patch.object(
                backend_client,
                "open_backend_https_url",
                side_effect=[handshake_timeout(), JsonResponse()],
            ) as opened,
            mock.patch.object(backend_client.time, "sleep"),
        ):
            result = backend_client.backend_request("GET", "/api/v1/orders/active", timeout=8)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opened.call_count, 2)

    def test_source_sync_with_its_long_timeout_is_not_retried(self):
        # 45 секунд на попытку с повтором давали 135,5 секунды ожидания.
        timeouts = []

        def open_url(request, timeout):
            timeouts.append(timeout)
            raise handshake_timeout()

        with (
            mock.patch.object(backend_client, "open_backend_https_url", side_effect=open_url),
            mock.patch.object(backend_client.time, "sleep") as sleep,
        ):
            with self.assertRaises(BackendTransportError):
                backend_client.sync_backend_sources()

        self.assertEqual(timeouts, [45])
        sleep.assert_not_called()


class BackendTransportPhaseIntegrationTests(unittest.TestCase):
    """Повтор через настоящий open_backend_https_url и поддельное соединение."""

    def setUp(self):
        FakeConnection.created = []
        FakeConnection.connect_plan = []
        http_client.reset_backend_connection()
        self.addCleanup(http_client.reset_backend_connection)
        for patcher in (
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
            mock.patch.object(backend_client.urllib.request, "getproxies", return_value={}),
            mock.patch.object(backend_client, "TAKSKLAD_BACKEND_BASE_URL", "https://api.taksklad.uz"),
            mock.patch.object(backend_client, "make_backend_headers", return_value={"Authorization": "Bearer x"}),
            mock.patch.object(backend_client.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def sent_requests():
        return sum(len(connection.requests) for connection in FakeConnection.created)

    def test_undo_is_not_repeated_when_reading_the_answer_fails(self):
        FakeConnection.created = []
        backend_client.backend_request("POST", "/api/v1/scans", {})
        FakeConnection.created[0].responses = [http.client.RemoteDisconnected("closed")]
        sent_before = self.sent_requests()

        with self.assertRaises(BackendTransportError):
            backend_client.undo_scan("item-1", "CODE")

        self.assertEqual(self.sent_requests() - sent_before, 1)

    def test_undo_is_repeated_when_the_connection_could_not_be_established(self):
        FakeConnection.connect_plan = [TimeoutError("_ssl.c:993: The handshake operation timed out")]

        result = backend_client.undo_scan("item-1", "CODE")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(FakeConnection.created), 2)
        self.assertEqual(self.sent_requests(), 1)

    def test_undo_is_repeated_when_the_reused_socket_was_already_dead(self):
        backend_client.backend_request("POST", "/api/v1/scans", {})
        FakeConnection.created[0].request_errors = [BrokenPipeError("broken pipe")]

        result = backend_client.undo_scan("item-1", "CODE")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(FakeConnection.created), 2)

    def test_scan_is_still_repeated_when_reading_the_answer_fails(self):
        backend_client.backend_request("POST", "/api/v1/scans", {})
        FakeConnection.created[0].responses = [http.client.RemoteDisconnected("closed")]
        sent_before = self.sent_requests()

        result = backend_client.create_scan("item-1", "CODE")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.sent_requests() - sent_before, 2)

    def test_order_complete_is_still_repeated_when_reading_the_answer_fails(self):
        backend_client.backend_request("POST", "/api/v1/scans", {})
        FakeConnection.created[0].responses = [http.client.RemoteDisconnected("closed")]

        result = backend_client.complete_order("order-1")

        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
