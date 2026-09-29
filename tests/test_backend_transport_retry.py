import http.client
import io
import socket
import ssl
import unittest
import urllib.error
from unittest import mock

from taksklad import backend_client
from taksklad.backend_client import BackendApiError, BackendTransportError
from taksklad.backend_events import backend_error_kind


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


class BackendTransportRetryTests(unittest.TestCase):
    @staticmethod
    def http_error(status):
        return urllib.error.HTTPError(
            "https://api.taksklad.uz/api/v1/scans",
            status,
            "synthetic",
            {},
            io.BytesIO(b'{"detail":"synthetic"}'),
        )

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


if __name__ == "__main__":
    unittest.main()
