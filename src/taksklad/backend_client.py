import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .config import (
    ORDER_DATE_COLUMN,
    SKLADBOT_REQUEST_ID_COLUMN,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    STATUS_COLUMN,
    STATUS_COMPLETED,
    STATUS_NOT_COMPLETED,
    TAKSKLAD_BACKEND_API_TOKEN,
    TAKSKLAD_BACKEND_BASE_URL,
    TAKSKLAD_BACKEND_ENABLED,
    TAKSKLAD_BACKEND_READ_ORDERS_ENABLED,
    TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
)
from .http_client import open_https_url
from .scan_quantities import scan_entries_for_codes
from .utils import parse_date_to_standard, split_codes


class BackendApiError(RuntimeError):
    def __init__(self, message, status_code=None, detail=None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

    @property
    def retryable(self):
        if self.status_code is None:
            return True
        return self.status_code in {408, 429, 500, 502, 503, 504}


def backend_enabled():
    return bool(TAKSKLAD_BACKEND_ENABLED)


def backend_read_orders_enabled():
    return bool(TAKSKLAD_BACKEND_ENABLED and TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)


def backend_configured():
    return backend_enabled() and bool(TAKSKLAD_BACKEND_BASE_URL)


def make_backend_headers():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "TakSklad-desktop",
    }
    if TAKSKLAD_BACKEND_API_TOKEN:
        headers["Authorization"] = f"Bearer {TAKSKLAD_BACKEND_API_TOKEN}"
    return headers


def backend_request(method, path, payload=None, timeout=None):
    if not TAKSKLAD_BACKEND_BASE_URL:
        raise BackendApiError("Backend URL не настроен")

    url = f"{TAKSKLAD_BACKEND_BASE_URL}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=make_backend_headers(),
        method=method,
    )
    try:
        with open_https_url(request, timeout=timeout or TAKSKLAD_BACKEND_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = read_error_detail(exc)
        raise BackendApiError(
            format_backend_error(exc.code, detail),
            status_code=exc.code,
            detail=detail,
        ) from exc
    except Exception as exc:
        logging.info("Backend request failed: %s %s", method, path, exc_info=True)
        raise BackendApiError(str(exc)) from exc


def read_error_detail(exc):
    try:
        raw = exc.read().decode("utf-8")
        if not raw:
            return ""
        payload = json.loads(raw)
        return payload.get("detail", payload)
    except Exception:
        return str(exc)


def format_backend_error(status_code, detail):
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error") or str(detail)
    else:
        message = str(detail or "")
    message = message.strip()
    if message:
        return f"Backend HTTP {status_code}: {message}"
    return f"Backend HTTP {status_code}"


def fetch_active_orders():
    return backend_request("GET", "/api/v1/orders/active")


def sync_backend_sources(sync_skladbot=True, wait_skladbot=True):
    query = urllib.parse.urlencode({
        "skladbot": "1" if sync_skladbot else "0",
        "wait_skladbot": "1" if wait_skladbot and sync_skladbot else "0",
    })
    timeout = max(TAKSKLAD_BACKEND_TIMEOUT_SECONDS, 45)
    return backend_request("POST", f"/api/v1/sync/sources?{query}", timeout=timeout)


def import_orders(records, filename=None, source="excel"):
    return backend_request(
        "POST",
        "/api/v1/imports",
        {
            "source": source,
            "filename": filename,
            "rows": records,
        },
    )


def preview_import_orders(records, filename=None, source="excel"):
    return backend_request(
        "POST",
        "/api/v1/imports/preview",
        {
            "source": source,
            "filename": filename,
            "rows": records,
        },
    )


def create_scan(order_item_id, code, workstation_id=None, scanned_at=None):
    payload = {
        "order_item_id": order_item_id,
        "code": code,
        "workstation_id": workstation_id,
    }
    if scanned_at:
        payload["scanned_at"] = scanned_at
    return backend_request("POST", "/api/v1/scans", payload)


def lookup_kiz_availability(code, order_item_id=""):
    query = urllib.parse.urlencode({
        "code": code,
        "order_item_id": order_item_id or "",
    })
    timeout = min(TAKSKLAD_BACKEND_TIMEOUT_SECONDS, 3)
    return backend_request("GET", f"/api/v1/kiz/availability?{query}", timeout=timeout)


def undo_scan(order_item_id, code, workstation_id=None, actor="desktop"):
    return backend_request(
        "POST",
        "/api/v1/scans/undo",
        {
            "order_item_id": order_item_id,
            "code": code,
            "workstation_id": workstation_id,
            "actor": actor,
        },
    )


def complete_order(order_id):
    return backend_request("POST", f"/api/v1/orders/{order_id}/complete")


def lookup_return_order(lookup_value):
    quoted = urllib.parse.urlencode({"lookup": lookup_value})
    return backend_request("GET", f"/api/v1/returns/lookup?{quoted}")


def fetch_returned_orders(limit=50):
    quoted = urllib.parse.urlencode({"limit": int(limit or 50)})
    return backend_request("GET", f"/api/v1/returns?{quoted}")


def mark_order_returned(order_id, return_reference="", returned_by="desktop", confirmed_items=None):
    return backend_request(
        "POST",
        f"/api/v1/returns/{order_id}",
        {
            "return_reference": return_reference,
            "returned_by": returned_by,
            "confirmed_items": confirmed_items or [],
        },
    )


def fetch_backend_sheet_data():
    orders = fetch_active_orders()
    rows = backend_orders_to_rows(orders)
    existing_codes = set()
    for row in rows:
        existing_codes.update(split_codes(row.get("Отсканированные коды")))
    return rows, None, existing_codes


def backend_orders_to_rows(orders):
    rows = []
    for order in orders or []:
        rows.extend(backend_order_to_rows(order))
    return rows


def backend_order_to_rows(order):
    rows = []
    raw_order = order if isinstance(order, dict) else {}
    request_number = raw_order.get("skladbot_request_number") or ""
    request_id = raw_order.get("skladbot_request_id") or ""
    status = desktop_status(raw_order.get("status"))

    for item in raw_order.get("items") or []:
        codes = item.get("scan_codes") or []
        if not codes:
            codes = split_codes(item.get("Отсканированные коды"))
        scan_entries = item.get("scan_entries") or scan_entries_for_codes(codes)
        row = {
            ORDER_DATE_COLUMN: date_to_display(raw_order.get("order_date")),
            "Тип оплаты": raw_order.get("payment_type") or "",
            "Клиент": raw_order.get("client") or "",
            "Адрес": raw_order.get("address") or "",
            "Торговый представитель": raw_order.get("representative") or "",
            "Товары": item.get("product") or "",
            "Кол-во ШТ": item.get("quantity_pieces") or 0,
            "Кол-во блок": item.get("quantity_blocks") or 0,
            "Цена за блок": item.get("block_price") or 0,
            "Сумма позиции": item.get("line_total") or 0,
            "Отсканированные коды": "\n".join(codes),
            STATUS_COLUMN: desktop_status(item.get("status")) if item.get("status") else status,
            SKLADBOT_REQUEST_NUMBER_COLUMN: request_number,
            SKLADBOT_REQUEST_ID_COLUMN: request_id,
            "_backend_order_id": raw_order.get("id") or "",
            "_backend_order_item_id": item.get("id") or "",
            "_existing_scanned_codes": list(codes),
            "_existing_scan_entries": list(scan_entries),
            "_backend_scanned_blocks": item.get("scanned_blocks") or 0,
        }
        rows.append(row)
    return rows


def desktop_status(value):
    text = str(value or "").strip().lower()
    if text in {"completed", "done", "closed", "выполнено", "готово"}:
        return STATUS_COMPLETED
    return STATUS_NOT_COMPLETED


def date_to_display(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    return parse_date_to_standard(value)
