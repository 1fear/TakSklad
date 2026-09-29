import http.client
import unittest
import urllib.error
import urllib.request
from unittest import mock

from taksklad import backend_client, http_client


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}', headers=None):
        self.status = status
        self.reason = "synthetic"
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body


class FakeSocket:
    def settimeout(self, _value):
        pass


class FakeConnection:
    created = []
    # По одному элементу на каждый вызов connect(): исключение или None.
    connect_plan = []

    def __init__(self, host, timeout=None, context=None):
        self.host = host
        self.timeout = timeout
        self.closed = False
        self.sock = None
        self.connects = 0
        self.requests = []
        self.request_errors = []
        self.responses = []
        FakeConnection.created.append(self)

    def connect(self):
        self.connects += 1
        outcome = FakeConnection.connect_plan.pop(0) if FakeConnection.connect_plan else None
        if outcome is not None:
            raise outcome
        self.sock = FakeSocket()

    def request(self, method, url, body=None, headers=None):
        if self.request_errors:
            raise self.request_errors.pop(0)
        self.requests.append((method, url, body, dict(headers or {})))

    def getresponse(self):
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return FakeResponse()

    def close(self):
        self.closed = True


class BackendKeepAliveTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.created = []
        FakeConnection.connect_plan = []
        http_client.reset_backend_connection()
        self.addCleanup(http_client.reset_backend_connection)
        # Прокси окружения разработчика не должен менять выбор пути в тестах.
        no_proxy = mock.patch.object(urllib.request, "getproxies", return_value={})
        no_proxy.start()
        self.addCleanup(no_proxy.stop)

    @staticmethod
    def request(path="/api/v1/scans"):
        return urllib.request.Request(
            f"https://api.taksklad.uz{path}",
            data=b"{}",
            headers={"Authorization": "Bearer x"},
            method="POST",
        )

    def test_second_request_reuses_the_open_connection(self):
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            with http_client.open_backend_https_url(self.request(), timeout=8) as second:
                second.read()

        self.assertEqual(len(FakeConnection.created), 1)
        self.assertEqual(len(FakeConnection.created[0].requests), 2)
        method, target, body, headers = FakeConnection.created[0].requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(target, "/api/v1/scans")
        self.assertEqual(body, b"{}")
        self.assertEqual(headers.get("Authorization"), "Bearer x")

    def test_broken_connection_is_dropped_so_the_next_call_opens_a_fresh_one(self):
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [http.client.RemoteDisconnected("closed")]
            with self.assertRaises(Exception):
                http_client.open_backend_https_url(self.request(), timeout=8)
            with http_client.open_backend_https_url(self.request(), timeout=8) as third:
                third.read()

        self.assertEqual(len(FakeConnection.created), 2)
        self.assertTrue(FakeConnection.created[0].closed)

    def test_error_status_is_raised_as_http_error_with_body(self):
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [
                FakeResponse(status=409, body=b'{"detail":"already scanned"}')
            ]
            with self.assertRaises(urllib.error.HTTPError) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertEqual(raised.exception.code, 409)
        self.assertIn(b"already scanned", raised.exception.read())

    def test_system_https_proxy_sends_the_request_through_urllib(self):
        # Раньше urlopen брал прокси из окружения и реестра Windows, а
        # HTTPSConnection идёт напрямую и на складе за прокси не достучится.
        request = self.request()
        sentinel = object()
        for proxies in ({"https": "http://proxy.local:3128"}, {"all": "http://proxy.local:3128"}):
            with self.subTest(proxies=proxies):
                with (
                    mock.patch.object(urllib.request, "getproxies", return_value=proxies),
                    mock.patch.object(urllib.request, "proxy_bypass", return_value=False),
                    mock.patch.object(http_client, "open_https_url", return_value=sentinel) as via_urllib,
                    mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
                ):
                    result = http_client.open_backend_https_url(request, timeout=8)

                self.assertIs(result, sentinel)
                via_urllib.assert_called_once_with(request, 8)
                self.assertEqual(FakeConnection.created, [])

    def test_backend_host_in_proxy_bypass_keeps_the_persistent_connection(self):
        with (
            mock.patch.object(urllib.request, "getproxies", return_value={"https": "http://proxy.local:3128"}),
            mock.patch.object(urllib.request, "proxy_bypass", return_value=True) as bypass,
            mock.patch.object(http_client, "open_https_url") as via_urllib,
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as response:
                response.read()

        bypass.assert_called_once_with("api.taksklad.uz")
        via_urllib.assert_not_called()
        self.assertEqual(len(FakeConnection.created), 1)

    def test_http_only_proxy_does_not_divert_https_backend_traffic(self):
        with (
            mock.patch.object(urllib.request, "getproxies", return_value={"http": "http://proxy.local:3128"}),
            mock.patch.object(http_client, "open_https_url") as via_urllib,
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as response:
                response.read()

        via_urllib.assert_not_called()
        self.assertEqual(len(FakeConnection.created), 1)

    def test_without_proxy_the_persistent_connection_is_used(self):
        with (
            mock.patch.object(http_client, "open_https_url") as via_urllib,
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as response:
                response.read()

        via_urllib.assert_not_called()
        self.assertEqual(len(FakeConnection.created), 1)

    def test_redirect_with_empty_body_is_an_http_error_not_an_accepted_scan(self):
        # 302 с пустым телом раньше проходил как успех и превращался в {}:
        # скан считался принятым, хотя backend его не видел.
        for status in (302, 304):
            with self.subTest(status=status):
                FakeConnection.created = []
                http_client.reset_backend_connection()
                with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
                    with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                        first.read()
                    FakeConnection.created[0].responses = [FakeResponse(status=status, body=b"")]
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        http_client.open_backend_https_url(self.request(), timeout=8)

                self.assertEqual(raised.exception.code, status)

    def test_redirect_reaches_the_caller_as_backend_error_with_status(self):
        with (
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
            mock.patch.object(backend_client, "TAKSKLAD_BACKEND_BASE_URL", "https://api.taksklad.uz"),
            mock.patch.object(backend_client, "make_backend_headers", return_value={}),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [FakeResponse(status=302, body=b"")]
            with self.assertRaises(backend_client.BackendApiError) as raised:
                backend_client.backend_request_page("POST", "/api/v1/scans", payload={})

        self.assertEqual(raised.exception.status_code, 302)
        self.assertNotIsInstance(raised.exception, backend_client.BackendTransportError)

    def test_no_content_answer_is_still_a_success(self):
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [FakeResponse(status=204, body=b"")]
            with http_client.open_backend_https_url(self.request(), timeout=8) as second:
                body = second.read()

        self.assertEqual(body, b"")

    def test_connection_idle_longer_than_the_limit_is_replaced_before_use(self):
        # Traefik и NAT закрывают простаивающие соединения молча: запрос по
        # такому сокету падает обрывом, поэтому старое соединение не берём.
        clock = [1000.0]
        with (
            mock.patch.object(http_client.time, "monotonic", lambda: clock[0]),
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            clock[0] += 46
            with http_client.open_backend_https_url(self.request(), timeout=8) as second:
                second.read()

        self.assertEqual(len(FakeConnection.created), 2)
        self.assertTrue(FakeConnection.created[0].closed)
        self.assertEqual(len(FakeConnection.created[1].requests), 1)

    def test_connection_used_within_the_limit_is_reused(self):
        clock = [1000.0]
        with (
            mock.patch.object(http_client.time, "monotonic", lambda: clock[0]),
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            clock[0] += 44
            with http_client.open_backend_https_url(self.request(), timeout=8) as second:
                second.read()

        self.assertEqual(len(FakeConnection.created), 1)

    def test_every_request_restarts_the_idle_clock(self):
        # Пачка событий длиннее 45 секунд в сумме не ломает соединение, пока
        # паузы между запросами короткие.
        clock = [1000.0]
        with (
            mock.patch.object(http_client.time, "monotonic", lambda: clock[0]),
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            for _ in range(4):
                with http_client.open_backend_https_url(self.request(), timeout=8) as response:
                    response.read()
                clock[0] += 40

        self.assertEqual(len(FakeConnection.created), 1)
        self.assertEqual(len(FakeConnection.created[0].requests), 4)

    def test_error_status_answer_also_restarts_the_idle_clock(self):
        clock = [1000.0]
        with (
            mock.patch.object(http_client.time, "monotonic", lambda: clock[0]),
            mock.patch.object(http.client, "HTTPSConnection", FakeConnection),
        ):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            clock[0] += 40
            FakeConnection.created[0].responses = [FakeResponse(status=409, body=b"{}")]
            with self.assertRaises(urllib.error.HTTPError):
                http_client.open_backend_https_url(self.request(), timeout=8)
            clock[0] += 40
            with http_client.open_backend_https_url(self.request(), timeout=8) as third:
                third.read()

        self.assertEqual(len(FakeConnection.created), 1)

    def test_connect_failure_is_marked_as_failed_before_the_request_was_sent(self):
        # Соединение или TLS-рукопожатие не установились: сервер запроса не видел.
        FakeConnection.connect_plan = [TimeoutError("_ssl.c:993: The handshake operation timed out")]
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with self.assertRaises(TimeoutError) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertTrue(http_client.failed_before_send(raised.exception))
        self.assertEqual(FakeConnection.created[0].requests, [])
        self.assertTrue(FakeConnection.created[0].closed)

    def test_send_failure_on_a_reused_socket_is_marked_as_failed_before_send(self):
        # Сервер закрыл простаивающий сокет: запрос не ушёл целиком, выполнить
        # его сервер не мог.
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].request_errors = [BrokenPipeError("broken pipe")]
            with self.assertRaises(BrokenPipeError) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertTrue(http_client.failed_before_send(raised.exception))

    def test_response_read_failure_is_not_marked_as_failed_before_send(self):
        # Запрос ушёл целиком: что сервер успел выполнить, неизвестно.
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [http.client.RemoteDisconnected("closed")]
            with self.assertRaises(http.client.RemoteDisconnected) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertFalse(http_client.failed_before_send(raised.exception))

    def test_response_body_read_failure_is_not_marked_as_failed_before_send(self):
        class BrokenBody(FakeResponse):
            def read(self):
                raise http.client.IncompleteRead(b"{")

        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            with http_client.open_backend_https_url(self.request(), timeout=8) as first:
                first.read()
            FakeConnection.created[0].responses = [BrokenBody()]
            with self.assertRaises(http.client.IncompleteRead) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertFalse(http_client.failed_before_send(raised.exception))

    def test_an_open_connection_is_not_connected_a_second_time(self):
        with mock.patch.object(http.client, "HTTPSConnection", FakeConnection):
            for _ in range(3):
                with http_client.open_backend_https_url(self.request(), timeout=8) as response:
                    response.read()

        self.assertEqual(FakeConnection.created[0].connects, 1)

    def test_url_error_from_the_urllib_path_is_marked_as_failed_before_send(self):
        # urllib оборачивает в URLError только ошибки соединения и отправки,
        # ошибки чтения ответа он отдаёт как есть.
        with (
            mock.patch.object(urllib.request, "getproxies", return_value={"https": "http://proxy.local:3128"}),
            mock.patch.object(urllib.request, "proxy_bypass", return_value=False),
            mock.patch.object(
                http_client,
                "open_https_url",
                side_effect=urllib.error.URLError("proxy refused"),
            ),
        ):
            with self.assertRaises(urllib.error.URLError) as raised:
                http_client.open_backend_https_url(self.request(), timeout=8)

        self.assertTrue(http_client.failed_before_send(raised.exception))


if __name__ == "__main__":
    unittest.main()
