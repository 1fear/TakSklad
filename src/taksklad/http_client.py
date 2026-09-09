import http.client
import io
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

import certifi

from .utils import normalize_text


HTTPS_CONTEXT = None
# Соединение живёт в своём потоке: очередь событий синхронизируется в одном
# потоке и выигрывает от переиспользования, а параллельные вызовы из других
# потоков не ждут чужого запроса.
BACKEND_CONNECTIONS = threading.local()


def get_https_context():
    global HTTPS_CONTEXT
    if HTTPS_CONTEXT is None:
        HTTPS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return HTTPS_CONTEXT


def open_https_url(request, timeout):
    url = request.full_url if isinstance(request, urllib.request.Request) else normalize_text(request)
    kwargs = {"timeout": timeout}
    if urllib.parse.urlparse(url).scheme.lower() == "https":
        kwargs["context"] = get_https_context()
    return urllib.request.urlopen(request, **kwargs)


class KeepAliveResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.code = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def reset_backend_connection():
    connection = getattr(BACKEND_CONNECTIONS, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
    BACKEND_CONNECTIONS.connection = None
    BACKEND_CONNECTIONS.host = None


def _backend_connection(host, timeout):
    connection = getattr(BACKEND_CONNECTIONS, "connection", None)
    if connection is not None and getattr(BACKEND_CONNECTIONS, "host", None) == host:
        connection.timeout = timeout
        socket = getattr(connection, "sock", None)
        if socket is not None:
            socket.settimeout(timeout)
        return connection
    reset_backend_connection()
    connection = http.client.HTTPSConnection(host, timeout=timeout, context=get_https_context())
    BACKEND_CONNECTIONS.connection = connection
    BACKEND_CONNECTIONS.host = host
    return connection


def open_backend_https_url(request, timeout):
    """Запрос к backend по постоянному соединению.

    Каждое рукопожатие это отдельный шанс упереться в дрожащий канал склада,
    поэтому пачка событий очереди должна укладываться в одно соединение.
    """
    url = request.full_url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        return open_https_url(request, timeout)

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    connection = _backend_connection(parsed.netloc, timeout)
    try:
        connection.request(
            request.get_method(),
            target,
            body=request.data,
            headers=dict(request.header_items()),
        )
        response = connection.getresponse()
        # Тело читается целиком сразу: недочитанный ответ делает соединение
        # непригодным для следующего запроса.
        body = response.read()
    except Exception:
        reset_backend_connection()
        raise

    status = int(getattr(response, "status", 0))
    if status >= 400:
        raise urllib.error.HTTPError(
            url,
            status,
            getattr(response, "reason", ""),
            response.headers,
            io.BytesIO(body),
        )
    return KeepAliveResponse(status, response.headers, body)
