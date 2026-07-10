import logging
import urllib.parse

import httpx

from .redaction import redact_secrets


TELEGRAM_BUTTON_SHIPMENT_DATE = "Дата отгрузки"
TELEGRAM_BUTTON_LOGISTICS_REPORT = "Отчёт логистики"
TELEGRAM_BUTTON_KIZ_BY_FILES = "Выгрузка КИЗов"
TELEGRAM_BUTTON_STATUS = "Статус"
TELEGRAM_BUTTON_IMPORTS = "Последние импорты"
TELEGRAM_BUTTON_MANUAL = "Ручное управление"


def telegram_main_reply_keyboard():
    return {
        "keyboard": [
            [{"text": TELEGRAM_BUTTON_LOGISTICS_REPORT}, {"text": TELEGRAM_BUTTON_KIZ_BY_FILES}],
            [{"text": TELEGRAM_BUTTON_STATUS}, {"text": TELEGRAM_BUTTON_IMPORTS}],
            [{"text": TELEGRAM_BUTTON_SHIPMENT_DATE}, {"text": TELEGRAM_BUTTON_MANUAL}],
        ],
        "resize_keyboard": True,
    }


class TelegramApiClient:
    def __init__(self, token, timeout=20, file_timeout=120, http_client_module=httpx):
        self.token = token
        self.timeout = timeout
        self.file_timeout = file_timeout
        self.http = http_client_module

    def request(self, method, payload=None, timeout=None):
        with self.http.Client(timeout=timeout or self.timeout) as client:
            try:
                response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload or {})
                response.raise_for_status()
            except self.http.HTTPStatusError as exc:
                detail = redact_secrets(exc.response.text[:300] if exc.response is not None else "")
                raise RuntimeError(
                    f"Telegram API request failed: {method}: HTTP {exc.response.status_code} {detail}"
                ) from None
            except self.http.HTTPError as exc:
                raise RuntimeError(f"Telegram API request failed: {method}: {exc.__class__.__name__}") from None
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(redact_secrets(data))
            return data.get("result")

    def configure_menu(self):
        self.request("deleteMyCommands", {})
        self.request("setChatMenuButton", {"menu_button": {"type": "default"}})

    def poll_updates(self, offset, poll_timeout):
        return self.request(
            "getUpdates",
            {
                "offset": offset + 1 if offset else None,
                "timeout": poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=poll_timeout + 5,
        ) or []

    def file_info(self, file_id):
        file_id = str(file_id or "").strip()
        if not file_id:
            raise ValueError("Telegram не передал file_id документа")
        result = self.request("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not str(result.get("file_path") or "").strip():
            raise RuntimeError("Telegram не вернул путь к файлу")
        return result

    def download_file(self, file_id, destination_path, max_file_size=0):
        file_path = str(self.file_info(file_id).get("file_path") or "").strip()
        quoted_path = urllib.parse.quote(file_path, safe="/")
        url = f"https://api.telegram.org/file/bot{self.token}/{quoted_path}"
        with self.http.Client(timeout=self.file_timeout, follow_redirects=True) as client:
            try:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    total = 0
                    with open(destination_path, "wb") as output:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if max_file_size and total > max_file_size:
                                raise ValueError("Файл слишком большой для Telegram import")
                            output.write(chunk)
            except self.http.HTTPError:
                raise RuntimeError("Не удалось скачать файл из Telegram") from None

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text[:3900]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def answer_callback_query(self, callback_query_id, text=""):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        return self.request("answerCallbackQuery", payload)

    def send_document(self, chat_id, content, filename, caption=""):
        with self.http.Client(timeout=self.file_timeout) as client:
            files = {"document": (filename, content)}
            data = {"chat_id": chat_id, "caption": caption[:1000]}
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument", data=data, files=files,
                )
                response.raise_for_status()
            except self.http.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else ""
                detail = redact_secrets(exc.response.text[:300] if exc.response is not None else "")
                raise RuntimeError(
                    f"Telegram API request failed: sendDocument: HTTP {status_code} {detail}"
                ) from None
            except self.http.HTTPError as exc:
                raise RuntimeError(
                    f"Telegram API request failed: sendDocument: {exc.__class__.__name__}"
                ) from None
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(redact_secrets(payload))
            return payload.get("result")


class BackendApiClient:
    def __init__(self, base_url, token="", timeout=20, import_timeout=120, file_timeout=120, http_client_module=httpx):
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.import_timeout = import_timeout
        self.file_timeout = file_timeout
        self.http = http_client_module

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path, params=None):
        with self.http.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{path}", params=params or {}, headers=self._headers())
            response.raise_for_status()
            return response.json()

    def get_bytes(self, path, params=None):
        with self.http.Client(timeout=self.file_timeout) as client:
            response = client.get(f"{self.base_url}{path}", params=params or {}, headers=self._headers())
            response.raise_for_status()
            return response.content, response.headers

    def post(self, path, payload=None):
        timeout = self.import_timeout if path == "/api/v1/imports" else self.timeout
        with self.http.Client(timeout=timeout) as client:
            response = client.post(f"{self.base_url}{path}", json=payload or {}, headers=self._headers())
            response.raise_for_status()
            return response.json()


class TelegramProcessorPorts:
    """Explicit injectable I/O ports shared by independently usable processors."""

    def __init__(
        self,
        *,
        telegram_api_client=None,
        backend_api_client=None,
        session_factory=None,
        excel_import_parser=None,
        skladbot_report_module=None,
        daily_reconciliation_callback=None,
        token="",
        backend_url="http://backend-api:8000",
        backend_token="",
        timeout=20,
        import_timeout=120,
        file_timeout=120,
        max_file_size=20 * 1024 * 1024,
        allowed_chat_ids=None,
        admin_chat_ids=None,
        state_store=None,
        owner=None,
    ):
        self._processor_owner = owner
        self.telegram_api_client = telegram_api_client
        self.backend_api_client = backend_api_client
        self.session_factory = session_factory
        self.excel_import_parser = excel_import_parser
        self.skladbot_report_module = skladbot_report_module
        self.daily_reconciliation_callback = daily_reconciliation_callback
        self.token = token
        self.backend_url = backend_url
        self.backend_token = backend_token
        self.timeout = timeout
        self.import_timeout = import_timeout
        self.file_timeout = file_timeout
        self.max_file_size = max_file_size
        self.allowed_chat_ids = set(allowed_chat_ids or ())
        self.admin_chat_ids = set(admin_chat_ids or ())
        self._processor_state_store = state_store if state_store is not None else {}
        self.manual_flow_cache = {}
        self.offset = 0
        self.bot_menu_ready = False

    def __getattribute__(self, name):
        if not name.startswith("_"):
            try:
                owner = object.__getattribute__(self, "_processor_owner")
            except AttributeError:
                owner = None
            if owner is not None and name in getattr(owner, "__dict__", {}):
                return owner.__dict__[name]
        return object.__getattribute__(self, name)

    @property
    def configured(self):
        return bool(getattr(self, "token", "") or getattr(self, "telegram_api_client", None))

    def _telegram_client(self):
        client = getattr(self, "telegram_api_client", None)
        if client is None:
            client = TelegramApiClient(
                getattr(self, "token", ""), getattr(self, "timeout", 20),
                getattr(self, "file_timeout", 120), getattr(self, "http_client_module", httpx),
            )
        return client

    def _backend_client(self):
        client = getattr(self, "backend_api_client", None)
        if client is None:
            client = BackendApiClient(
                self.backend_url, self.backend_token, self.timeout, self.import_timeout,
                self.file_timeout, getattr(self, "http_client_module", httpx),
            )
        return client

    def telegram_request(self, method, payload=None, timeout=None):
        return self._telegram_client().request(method, payload, timeout)

    def poll_updates(self, offset, poll_timeout):
        return self._telegram_client().poll_updates(offset, poll_timeout)

    def telegram_file_info(self, file_id):
        return self._telegram_client().file_info(file_id)

    def download_telegram_document(self, document, destination_path):
        return self._telegram_client().download_file(
            (document or {}).get("file_id"), destination_path, getattr(self, "max_file_size", 0),
        )

    def ensure_bot_menu(self):
        if getattr(self, "bot_menu_ready", False):
            return
        try:
            self._telegram_client().configure_menu()
            self.bot_menu_ready = True
        except Exception:
            logging.warning("Telegram worker: failed to configure bot menu", exc_info=True)

    def backend_get(self, path, params=None):
        return self._backend_client().get(path, params)

    def backend_get_bytes(self, path, params=None):
        return self._backend_client().get_bytes(path, params)

    def backend_post(self, path, payload=None):
        return self._backend_client().post(path, payload)

    def send_message(self, chat_id, text, reply_markup=None):
        return self._telegram_client().send_message(chat_id, text, reply_markup)

    def send_document(self, chat_id, content, filename, caption=""):
        return self._telegram_client().send_document(chat_id, content, filename, caption)

    def safe_send_document(self, chat_id, content, filename, caption=""):
        try:
            return self.send_document(chat_id, content, filename, caption=caption)
        except Exception as exc:
            logging.warning("Telegram worker: failed to send document: %s", redact_secrets(exc))
            self.safe_send_message(chat_id, f"Не удалось отправить файл: {filename}")
            return None

    def safe_send_message(self, chat_id, text, reply_markup=None):
        try:
            if reply_markup is None:
                return self.send_message(chat_id, text)
            return self.send_message(chat_id, text, reply_markup)
        except Exception:
            logging.warning("Telegram worker: failed to send message", exc_info=True)
            return None

    def send_main_menu(self, chat_id, text=""):
        lines = [
            str(text or "").strip() or "Меню TakSklad",
            "",
            "Excel-файл можно просто отправить в этот чат. Бот попросит дату отгрузки перед импортом.",
        ]
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_main_reply_keyboard())

    def send_date_help(self, chat_id):
        current_date = self.get_chat_shipment_date(chat_id)
        self.safe_send_message(chat_id, "\n".join([
            "Дата отгрузки задаётся после загрузки каждого Excel-файла.",
            "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
            "Пример: 09.06.2026",
            f"Сохранённая дата чата: {current_date or 'не задана'}",
        ]))

    def answer_callback_query(self, callback_query_id, text=""):
        callback_query_id = str(callback_query_id or "").strip()
        if not callback_query_id:
            return None
        return self._telegram_client().answer_callback_query(
            callback_query_id, str(text or "").strip(),
        )

    def is_allowed_chat(self, chat_id):
        return str(chat_id or "") in getattr(self, "allowed_chat_ids", set())

    def is_admin_chat(self, chat_id):
        return str(chat_id or "") in getattr(self, "admin_chat_ids", set())

    def ensure_admin_chat(self, chat_id):
        allowed = self.is_admin_chat(chat_id)
        if not allowed:
            self.safe_send_message(chat_id, "Действие доступно только администратору.")
        return allowed

    def get_chat_state(self, chat_id):
        return dict(self._processor_state_store.get(str(chat_id or ""), {}))

    def save_chat_state(self, chat_id, state):
        self._processor_state_store[str(chat_id or "")] = dict(state or {})

    def get_chat_shipment_date(self, chat_id):
        return str(self.get_chat_state(chat_id).get("shipment_date") or "")


ExternalHttpError = httpx.HTTPError
ExternalTimeoutError = httpx.TimeoutException


class TelegramProcessorDelegate:
    """Composition bridge: processors depend on a port object, not a worker."""

    def __init__(self, *, ports=None, owner=None, **port_dependencies):
        self._processor_ports = ports or TelegramProcessorPorts(**port_dependencies)
        self._processor_owner = owner

    def __getattribute__(self, name):
        if not name.startswith("_"):
            try:
                owner = object.__getattribute__(self, "_processor_owner")
            except AttributeError:
                owner = None
            if owner is not None and name in getattr(owner, "__dict__", {}):
                return owner.__dict__[name]
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        if name == "_processor_ports":
            raise AttributeError(name)
        ports = object.__getattribute__(self, "_processor_ports")
        if name in ports.__dict__:
            return ports.__dict__[name]
        descriptor = getattr(TelegramProcessorPorts, name, None)
        if descriptor is not None and hasattr(descriptor, "__get__"):
            return descriptor.__get__(self, type(self))
        return getattr(ports, name)
