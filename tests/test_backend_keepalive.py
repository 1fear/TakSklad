import http.client
import unittest
import urllib.error
import urllib.request
from unittest import mock

from taksklad import http_client


class FakeResponse:
    def __init__(self, status=200, body=b'{"ok":true}', headers=None):
        self.status = status
        self.reason = "synthetic"
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body


class FakeConnection:
    created = []

    def __init__(self, host, timeout=None, context=None):
        self.host = host
        self.timeout = timeout
        self.closed = False
        self.requests = []
        self.responses = []
        FakeConnection.created.append(self)

    def request(self, method, url, body=None, headers=None):
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
        http_client.reset_backend_connection()
        self.addCleanup(http_client.reset_backend_connection)

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


if __name__ == "__main__":
    unittest.main()
