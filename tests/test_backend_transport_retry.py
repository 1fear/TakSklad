import http.client
import io
import unittest
import urllib.error
from unittest import mock

from taksklad import backend_client
from taksklad.backend_client import BackendApiError


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


if __name__ == "__main__":
    unittest.main()
