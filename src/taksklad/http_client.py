import http.client
import io
import ssl
import threading
import time
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
# Traefik и NAT закрывают простаивающие соединения молча, а главный поток
# держит своё долго: запрос по мёртвому сокету падает обрывом. Соединение,
# которым не пользовались дольше этого срока, пересоздаётся до запроса.
BACKEND_CONNECTION_IDLE_LIMIT_SECONDS = 45


# Фаза оборвавшегося запроса. Повтор изменяющего запроса безопасен, только если
# сервер его заведомо не выполнял: соединение не установилось, либо запрос не
# ушёл целиком. Ошибка чтения ответа этого не гарантирует.
FAILURE_PHASE_ATTRIBUTE = "taksklad_failure_phase"
FAILURE_PHASE_BEFORE_SEND = "before_send"


def mark_failed_before_send(exc):
    try:
        setattr(exc, FAILURE_PHASE_ATTRIBUTE, FAILURE_PHASE_BEFORE_SEND)
    except Exception:
        pass
    return exc


def failed_before_send(exc):
    return getattr(exc, FAILURE_PHASE_ATTRIBUTE, None) == FAILURE_PHASE_BEFORE_SEND


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
    BACKEND_CONNECTIONS.last_used = None


def _backend_connection_is_fresh():
    last_used = getattr(BACKEND_CONNECTIONS, "last_used", None)
    if last_used is None:
        return False
    return time.monotonic() - last_used <= BACKEND_CONNECTION_IDLE_LIMIT_SECONDS


def _backend_connection(host, timeout):
    connection = getattr(BACKEND_CONNECTIONS, "connection", None)
    if (
        connection is not None
        and getattr(BACKEND_CONNECTIONS, "host", None) == host
        and _backend_connection_is_fresh()
    ):
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


def system_proxy_applies(netloc):
    """Пойдёт ли запрос к хосту через системный прокси, как это делал urlopen.

    urllib берёт прокси из окружения и реестра Windows, а HTTPSConnection
    ходит напрямую и за корпоративным прокси не достучится. Если прокси
    задан для https и хост не в исключениях, запрос обязан идти прежним
    путём через urllib, без постоянного соединения.
    """
    try:
        proxies = urllib.request.getproxies()
        if not (proxies.get("https") or proxies.get("all")):
            return False
        return not urllib.request.proxy_bypass(netloc)
    except Exception:
        # Настройки прокси не прочитались: надёжнее прежний путь urllib,
        # чем идти напрямую там, где прямого выхода может не быть.
        return True


def open_backend_https_url(request, timeout):
    """Запрос к backend по постоянному соединению.

    Каждое рукопожатие это отдельный шанс упереться в дрожащий канал склада,
    поэтому пачка событий очереди должна укладываться в одно соединение.
    """
    url = request.full_url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or system_proxy_applies(parsed.netloc):
        try:
            return open_https_url(request, timeout)
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            # urllib оборачивает в URLError только ошибки соединения и
            # отправки, ошибки чтения ответа он отдаёт как есть.
            mark_failed_before_send(exc)
            raise

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    connection = _backend_connection(parsed.netloc, timeout)
    try:
        # Соединение открывается явно и отдельно от запроса: так видно, в какой
        # фазе оборвалось, а до отправки сервер запроса не получал.
        if getattr(connection, "sock", None) is None:
            connection.connect()
        connection.request(
            request.get_method(),
            target,
            body=request.data,
            headers=dict(request.header_items()),
        )
    except Exception as exc:
        # Ошибка соединения, рукопожатия или отправки (в том числе мгновенный
        # обрыв переиспользованного сокета): запрос целиком сервер не получил.
        reset_backend_connection()
        mark_failed_before_send(exc)
        raise
    try:
        response = connection.getresponse()
        # Тело читается целиком сразу: недочитанный ответ делает соединение
        # непригодным для следующего запроса.
        body = response.read()
    except Exception:
        reset_backend_connection()
        raise
    BACKEND_CONNECTIONS.last_used = time.monotonic()

    status = int(getattr(response, "status", 0))
    # Успех это только 2xx: редирект и прочее нештатное с пустым телом иначе
    # превращалось в {} и скан считался принятым, хотя backend его не видел.
    if not 200 <= status < 300:
        raise urllib.error.HTTPError(
            url,
            status,
            getattr(response, "reason", ""),
            response.headers,
            io.BytesIO(body),
        )
    return KeepAliveResponse(status, response.headers, body)
