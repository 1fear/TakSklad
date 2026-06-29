import os
import time
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .skladbot_worker import (
    SkladBotClient,
    business_timezone,
    business_today,
    env_float,
    env_int,
    extract_list_items,
    normalize_request_payload,
    normalize_text,
    parse_date,
    parse_int,
    request_list_value,
    sanitize_skladbot_error,
)


DEFAULT_DAILY_REPORT_REQUEST_TYPE_IDS = (3387, 3388, 3389, 3391, 3403)
SKLADBOT_DAILY_REPORT_REQUEST_TYPE_IDS_ENV = "SKLADBOT_DAILY_REPORT_REQUEST_TYPE_IDS"
REQUEST_CATEGORY_SHIPMENT = "Отгрузка"
REQUEST_CATEGORY_DEFECT_SHIPMENT = "Отгрузка в браке"
REQUEST_CATEGORY_RETURN = "Возврат"
REQUEST_CATEGORY_RECEIVING = "Приемка"
REQUEST_CATEGORY_OTHER = "Прочее"
REQUEST_CATEGORIES = (
    REQUEST_CATEGORY_SHIPMENT,
    REQUEST_CATEGORY_DEFECT_SHIPMENT,
    REQUEST_CATEGORY_RETURN,
    REQUEST_CATEGORY_RECEIVING,
    REQUEST_CATEGORY_OTHER,
)

REQUEST_HEADERS = [
    "ID",
    "Номер",
    "Категория",
    "Тип",
    "Статус",
    "В архиве",
    "Дата создания",
    "Дата обновления",
    "Дата выгрузки",
    "Юрлицо/точка",
    "Клиент SkladBot",
    "Адрес",
    "Комментарий",
    "Блоков план",
    "Блоков факт",
    "Отклонение",
    "Товаров",
    "Причина включения",
]

REQUEST_PRODUCT_HEADERS = [
    "Заявка",
    "ID заявки",
    "Тип",
    "Дата выгрузки",
    "Юрлицо/точка",
    "Товар",
    "Артикул",
    "Штрихкод",
    "Блоков план",
    "Принято факт",
    "Блоков факт",
    "Отклонение",
]

MOVEMENT_HEADERS = [
    "Направление",
    "Дата",
    "Заявка/документ",
    "Тип движения",
    "Клиент",
    "Товар",
    "Артикул",
    "Штрихкод",
    "Кол-во",
    "Короб",
    "Ячейка",
]

STOCK_HEADERS = [
    "Клиент",
    "Товар",
    "Артикул",
    "Штрихкод",
    "Остаток",
    "Обычный остаток",
    "Номинальный остаток",
    "Доступно",
]


def configured_request_type_ids(environ: dict[str, str] | None = None) -> list[int]:
    environ = environ or os.environ
    raw = normalize_text(environ.get(SKLADBOT_DAILY_REPORT_REQUEST_TYPE_IDS_ENV))
    if not raw:
        return []
    result = []
    for part in raw.replace(";", ",").split(","):
        value = parse_int(part)
        if value > 0 and value not in result:
            result.append(value)
    return result


def collect_skladbot_daily_report(
    report_date: date | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    client = client or SkladBotClient()
    report_date = report_date or business_today()
    generated_at = datetime.now(timezone.utc).astimezone(business_timezone())
    result = {
        "report_date": report_date,
        "generated_at": generated_at,
        "customer_id": getattr(client, "customer_id", None),
        "requests": [],
        "movements": [],
        "stock": {"total": 0, "rows": [], "raw": {}, "error": ""},
        "errors": [],
    }
    if not getattr(client, "configured", False):
        result["errors"].append("SKLADBOT_API_TOKEN is not configured")
        result["summary"] = summarize_daily_report(result)
        return result

    request_types = load_request_types(client, result["errors"])
    movements = fetch_daily_movements(client, report_date, result["errors"])
    result["movements"] = movements
    requests = fetch_daily_requests(
        client,
        report_date,
        request_types,
        result["errors"],
    )
    result["requests"] = requests
    result["stock"] = fetch_current_stock(client, result["errors"])
    result["summary"] = summarize_daily_report(result)
    return result


def load_request_types(client: Any, errors: list[str]) -> list[dict[str, Any]]:
    configured_ids = configured_request_type_ids()
    if configured_ids:
        return [{"id": type_id, "name": ""} for type_id in configured_ids]
    try:
        payload = client.get("/requests/filter/fields")
        request_types = extract_request_types(payload)
    except Exception as exc:
        errors.append(f"Не удалось получить типы заявок SkladBot: {sanitize_skladbot_error(exc)}")
        request_types = []
    if request_types:
        return request_types
    return [{"id": type_id, "name": ""} for type_id in DEFAULT_DAILY_REPORT_REQUEST_TYPE_IDS]


def extract_request_types(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for value in find_values_by_key(payload, {"types", "request_types", "requestTypes"}):
        if isinstance(value, dict):
            value = extract_list_items(value)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            type_id = parse_int(item.get("id") or item.get("value"))
            name = normalize_text(item.get("name") or item.get("title") or item.get("label") or item.get("type"))
            if type_id > 0 and (name or "request" not in normalize_text(item.get("group")).lower()):
                rows.append({"id": type_id, "name": name})
    seen = set()
    result = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        result.append(row)
    return result


def find_values_by_key(value: Any, names: set[str]) -> list[Any]:
    result = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in names:
                result.append(nested)
            result.extend(find_values_by_key(nested, names))
    elif isinstance(value, list):
        for item in value:
            result.extend(find_values_by_key(item, names))
    return result


def fetch_daily_requests(
    client: Any,
    report_date: date,
    request_types: list[dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    limit = max(1, env_int("SKLADBOT_DAILY_REPORT_REQUESTS_LIMIT", getattr(client, "limit", 500) or 500))
    default_detail_limit = max(250, limit * max(1, len(request_types)))
    detail_limit = max(1, env_int("SKLADBOT_DAILY_REPORT_DETAIL_LIMIT", default_detail_limit))
    request_delay = max(0.0, env_float("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", 3.0))
    result = []
    seen_ids = set()
    checked_details = 0
    for request_type in request_types:
        type_id = parse_int(request_type.get("id"))
        if type_id <= 0:
            continue
        try:
            list_payload = client.get("/requests", {
                "customer_id": getattr(client, "customer_id", None),
                "type_id": type_id,
                "limit": limit,
            })
            list_items = extract_list_items(list_payload)
            if request_delay:
                time.sleep(request_delay)
        except Exception as exc:
            errors.append(f"Не удалось получить список заявок type_id={type_id}: {sanitize_skladbot_error(exc)}")
            continue
        for list_item in report_date_request_list_items(list_items, report_date):
            if checked_details >= detail_limit:
                errors.append(f"Лимит детализации заявок достигнут: {detail_limit}")
                return result
            request_id = parse_int(request_list_value(list_item, "id"))
            if request_id <= 0 or request_id in seen_ids:
                continue
            try:
                detail = get_daily_request_detail(client, request_id, request_delay)
                checked_details += 1
            except Exception as exc:
                errors.append(f"Не удалось получить заявку {request_id}: {sanitize_skladbot_error(exc)}")
                continue
            if request_delay:
                time.sleep(request_delay)
            request = normalize_request_payload(list_item, detail)
            request["category"] = categorize_request_type(request.get("type") or request_type.get("name"))
            reasons = request_inclusion_reasons(
                request,
                report_date,
            )
            if not reasons:
                continue
            request["type_id"] = type_id
            request["include_reasons"] = reasons
            seen_ids.add(request_id)
            result.append(request)
    result.sort(key=lambda item: (
        category_sort_key(item.get("category")),
        parse_int(item.get("id")),
    ))
    return result


def get_daily_request_detail(client: Any, request_id: int, request_delay: float) -> Any:
    rate_limit_retries = max(0, env_int("SKLADBOT_DAILY_REPORT_429_RETRIES", 2))
    retry_seconds = max(request_delay, env_float("SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS", 15.0))
    for attempt in range(rate_limit_retries + 1):
        try:
            return client.get_request_detail(request_id)
        except Exception as exc:
            if attempt >= rate_limit_retries or not is_skladbot_rate_limit_error(exc):
                raise
            if retry_seconds:
                time.sleep(retry_seconds)
    raise RuntimeError(f"Не удалось получить заявку {request_id}")


def prioritize_request_list_items(list_items: list[Any], report_date: date) -> list[Any]:
    return sorted(list_items, key=lambda item: (
        0 if date_matches(request_list_value(item, "created_at", "createdAt"), report_date) else 1,
    ))


def report_date_request_list_items(list_items: list[Any], report_date: date) -> list[Any]:
    return [
        item
        for item in prioritize_request_list_items(list_items, report_date)
        if date_matches(request_list_value(item, "created_at", "createdAt"), report_date)
    ]


def is_skladbot_rate_limit_error(exc: Exception) -> bool:
    text = sanitize_skladbot_error(exc).lower()
    return "429" in text or "too many requests" in text


def request_inclusion_reasons(
    request: dict[str, Any],
    report_date: date,
) -> list[str]:
    if not request_is_completed_and_archived(request):
        return []
    if request_created_on_report_date(request, report_date):
        return ["создана"]
    return []


def request_is_completed_and_archived(request: dict[str, Any]) -> bool:
    return bool(request.get("is_completed") and request.get("archived"))


def request_created_on_report_date(request: dict[str, Any], report_date: date) -> bool:
    return date_matches(request_list_value(request, "created_at", "createdAt"), report_date)


def date_matches(value: Any, expected: date) -> bool:
    parsed = parse_date(value)
    return bool(parsed and parsed == expected)


def categorize_request_type(value: Any) -> str:
    text = normalize_text(value).lower().replace("ё", "е")
    is_outbound = "отгруз" in text or "расход" in text
    if is_outbound and "брак" in text:
        return REQUEST_CATEGORY_DEFECT_SHIPMENT
    if "возврат" in text:
        return REQUEST_CATEGORY_RETURN
    if is_outbound:
        return REQUEST_CATEGORY_SHIPMENT
    if "прием" in text or "приемка" in text:
        return REQUEST_CATEGORY_RECEIVING
    return REQUEST_CATEGORY_OTHER


def category_sort_key(value: Any) -> int:
    return {category: index for index, category in enumerate(REQUEST_CATEGORIES, start=1)}.get(normalize_text(value), 9)


def fetch_daily_movements(client: Any, report_date: date, errors: list[str]) -> list[dict[str, Any]]:
    limit = max(1, env_int("SKLADBOT_DAILY_REPORT_MOVEMENTS_LIMIT", 1000))
    result = []
    for movement_type, direction in (("in", "Приход"), ("out", "Расход")):
        try:
            payload = client.post("/warehouse/transactions", {
                "customer_id": getattr(client, "customer_id", None),
                "limit": limit,
                "type": movement_type,
                "from": report_date.isoformat(),
                "to": report_date.isoformat(),
            })
        except Exception as exc:
            errors.append(f"Не удалось получить движения {direction}: {sanitize_skladbot_error(exc)}")
            continue
        for item in extract_list_items(payload):
            if isinstance(item, dict):
                movement = normalize_movement(item, direction)
                if movement_on_report_date(movement, report_date):
                    result.append(movement)
    result.sort(key=lambda item: (normalize_text(item.get("date")), normalize_text(item.get("request_number"))))
    return result


def movement_on_report_date(movement: dict[str, Any], report_date: date) -> bool:
    return date_matches(movement.get("date"), report_date)


def normalize_movement(item: dict[str, Any], direction: str) -> dict[str, Any]:
    product = first_nested_dict(item, "product", "nomenclature", "product_data", "productData")
    customer = first_nested_dict(item, "customer", "client")
    box = first_nested_dict(item, "box")
    cell = first_nested_dict(item, "cell", "place", "location")
    return {
        "direction": direction,
        "date": first_text(item, "date", "created_at", "createdAt", "datetime", "created"),
        "request_number": first_text(item, "delivery_number", "request_number", "request", "document", "source"),
        "movement_type": first_text(item, "type", "movement_type", "operation"),
        "customer": nested_text(customer, "name", "title") or first_text(item, "customer", "client"),
        "product": nested_text(product, "name", "title") or first_text(item, "product", "name", "title"),
        "vendor_code": nested_text(product, "vendorCode", "vendor_code", "article", "sku") or first_text(item, "vendorCode", "vendor_code", "article", "sku"),
        "barcode": nested_text(product, "barcode") or first_text(item, "barcode"),
        "amount": first_int(item, "amount", "quantity", "count", "qty"),
        "box": nested_text(box, "name", "number", "title") or first_text(item, "box"),
        "cell": nested_text(cell, "name", "title", "code") or first_text(item, "cell", "place", "location"),
        "raw": item,
    }


def fetch_current_stock(client: Any, errors: list[str]) -> dict[str, Any]:
    products_stock = fetch_products_stock(client, errors)
    try:
        payload = client.post("/report/stock", {
            "customer_id": getattr(client, "customer_id", None),
            "with_details": True,
        })
    except Exception as exc:
        error = f"Не удалось получить остаток SkladBot: {sanitize_skladbot_error(exc)}"
        errors.append(error)
        if products_stock["rows"]:
            products_stock["error"] = error
            return products_stock
        return {"total": 0, "rows": [], "raw": {}, "error": error}
    if products_stock["rows"]:
        products_stock["raw"] = {"products": products_stock.get("raw") or {}, "report_stock": payload}
        products_stock["report_total"] = stock_total(payload, normalize_stock_rows(payload))
        return products_stock
    rows = normalize_stock_rows(payload)
    total = stock_total(payload, rows)
    return {"total": total, "rows": rows, "raw": payload, "error": ""}


def fetch_products_stock(client: Any, errors: list[str]) -> dict[str, Any]:
    try:
        payload = client.post("/products", {
            "customer_id": getattr(client, "customer_id", None),
            "limit": 1000,
        })
    except Exception as exc:
        errors.append(f"Не удалось получить товары SkladBot: {sanitize_skladbot_error(exc)}")
        return {"total": 0, "rows": [], "raw": {}, "error": ""}
    rows = normalize_stock_rows(payload)
    total = sum(parse_int(row.get("stock")) for row in rows)
    return {"total": total, "rows": rows, "raw": payload, "error": ""}


def normalize_stock_rows(payload: Any) -> list[dict[str, Any]]:
    rows = []
    for item in find_stock_like_dicts(payload):
        row = normalize_stock_row(item)
        if any(row.get(key) for key in ("product", "vendor_code", "barcode")) or row.get("stock") or row.get("available"):
            rows.append(row)
    seen = set()
    result = []
    for row in rows:
        key = (
            row.get("customer"),
            row.get("product"),
            row.get("vendor_code"),
            row.get("barcode"),
            row.get("stock"),
            row.get("available"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    result.sort(key=lambda item: (normalize_text(item.get("product")), normalize_text(item.get("barcode"))))
    return result


def find_stock_like_dicts(value: Any) -> list[dict[str, Any]]:
    result = []
    if isinstance(value, dict):
        keys = {normalize_text(key).lower() for key in value.keys()}
        if keys.intersection({"stock", "balance", "available", "quantity", "amount", "product", "product_data", "productdata"}):
            result.append(value)
        for nested in value.values():
            result.extend(find_stock_like_dicts(nested))
    elif isinstance(value, list):
        for item in value:
            result.extend(find_stock_like_dicts(item))
    return result


def normalize_stock_row(item: dict[str, Any]) -> dict[str, Any]:
    product = first_nested_dict(item, "product", "product_data", "productData", "nomenclature")
    customer = first_nested_dict(item, "customer", "client")
    return {
        "customer": nested_text(customer, "name", "title") or first_text(item, "customer", "client"),
        "product": nested_text(product, "name", "title") or first_text(item, "product", "name", "title"),
        "vendor_code": nested_text(product, "vendorCode", "vendor_code", "article", "sku") or first_text(item, "vendorCode", "vendor_code", "article", "sku"),
        "barcode": nested_text(product, "barcode") or first_text(item, "barcode"),
        "stock": first_int(item, "stock", "balance", "quantity", "amount", "count", "total"),
        "regular_stock": first_int(item, "regular_stock", "ordinary_stock", "stock_regular", "normal_stock"),
        "nominal_stock": first_int(item, "nominal_stock", "nominale_stock", "stock_nominal", "nominale"),
        "available": first_int(item, "available", "available_stock", "free", "free_stock"),
        "raw": item,
    }


def stock_total(payload: Any, rows: list[dict[str, Any]]) -> int:
    root_total = first_int(payload, "total", "stock", "balance", "amount", "quantity") if isinstance(payload, dict) else 0
    if root_total:
        return root_total
    row_total = sum(parse_int(row.get("stock")) for row in rows)
    return row_total


def first_nested_dict(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return {}


def nested_text(item: dict[str, Any], *keys: str) -> str:
    return first_text(item, *keys) if item else ""


def first_text(item: Any, *keys: str) -> str:
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            text = nested_text(value, "name", "title", "number", "code")
        else:
            text = normalize_text(value)
        if text:
            return text
    return ""


def first_int(item: Any, *keys: str) -> int:
    if not isinstance(item, dict):
        return 0
    for key in keys:
        if key not in item:
            continue
        value = parse_int(item.get(key))
        if value:
            return value
    return 0


def summarize_daily_report(report: dict[str, Any]) -> dict[str, Any]:
    requests = report.get("requests") or []
    movements = report.get("movements") or []
    category_counts = {category: 0 for category in REQUEST_CATEGORIES}
    type_counts: dict[str, int] = {}
    request_blocks_by_category = {category: 0 for category in REQUEST_CATEGORIES}
    for request in requests:
        category = normalize_text(request.get("category")) or REQUEST_CATEGORY_OTHER
        category_counts[category] = category_counts.get(category, 0) + 1
        type_name = normalize_text(request.get("type")) or "Без типа"
        type_counts[type_name] = type_counts.get(type_name, 0) + 1
        request_blocks_by_category[category] = request_blocks_by_category.get(category, 0) + request_report_blocks(request)
    movement_in = [item for item in movements if item.get("direction") == "Приход"]
    movement_out = [item for item in movements if item.get("direction") == "Расход"]
    return {
        "requests_total": len(requests),
        "category_counts": category_counts,
        "type_counts": type_counts,
        "request_blocks_by_category": request_blocks_by_category,
        "movements_total": len(movements),
        "movement_in_rows": len(movement_in),
        "movement_out_rows": len(movement_out),
        "movement_in_amount": sum(parse_int(item.get("amount")) for item in movement_in),
        "movement_out_amount": sum(parse_int(item.get("amount")) for item in movement_out),
        "stock_total": parse_int((report.get("stock") or {}).get("total")),
        "stock_rows": len((report.get("stock") or {}).get("rows") or []),
        "errors": len(report.get("errors") or []),
    }


def request_blocks(request: dict[str, Any]) -> int:
    return sum(parse_int(product.get("amount")) for product in request.get("products") or [])


def request_report_blocks(request: dict[str, Any]) -> int:
    category = normalize_text(request.get("category")) or REQUEST_CATEGORY_OTHER
    return sum(report_product_blocks(product, category) for product in request.get("products") or [])


def report_product_blocks(product: dict[str, Any], category: str) -> int:
    if normalize_text(category) == REQUEST_CATEGORY_RECEIVING:
        accepted_amount = parse_int(product.get("accepted_amount"))
        if product.get("accepted_amount_present"):
            return accepted_amount
        if accepted_amount > 0:
            return accepted_amount
    return parse_int(product.get("amount"))


def product_key(name: Any, vendor_code: Any = "", barcode: Any = "") -> str:
    aliases = product_aliases(name, vendor_code, barcode)
    return aliases[0] if aliases else ""


def product_aliases(name: Any, vendor_code: Any = "", barcode: Any = "") -> list[str]:
    aliases = []
    product_name = normalize_text(name).lower()
    if product_name:
        aliases.append(f"name:{product_name}")
    vendor = normalize_text(vendor_code).lower()
    if vendor:
        aliases.append(f"vendor:{vendor}")
    product_barcode = normalize_text(barcode).lower()
    if product_barcode:
        aliases.append(f"barcode:{product_barcode}")
    return aliases


def product_label(name: Any, vendor_code: Any = "", barcode: Any = "") -> str:
    return (
        normalize_text(name)
        or normalize_text(vendor_code)
        or normalize_text(barcode)
        or "Товар не найден"
    )


def product_breakdown_for_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    aliases_by_product: dict[str, str] = {}

    def ensure_product(name: Any, vendor_code: Any = "", barcode: Any = "") -> dict[str, Any]:
        aliases = product_aliases(name, vendor_code, barcode)
        key = next((
            aliases_by_product[alias]
            for alias in aliases
            if alias in aliases_by_product
        ), "")
        if not key:
            key = product_key(name, vendor_code, barcode)
        if not key:
            key = f"unknown:{len(products) + 1}"
        if key not in products:
            products[key] = {
                "key": key,
                "name": product_label(name, vendor_code, barcode),
                "ending_stock": 0,
                "inbound": 0,
                "outbound": 0,
                "defect_outbound": 0,
                "returns": 0,
            }
        elif normalize_text(name) and not normalize_text(products[key].get("name")):
            products[key]["name"] = product_label(name, vendor_code, barcode)
        for alias in aliases:
            aliases_by_product[alias] = key
        return products[key]

    for row in (report.get("stock") or {}).get("rows") or []:
        product = ensure_product(row.get("product"), row.get("vendor_code"), row.get("barcode"))
        product["ending_stock"] += parse_int(row.get("stock"))

    for request in report.get("requests") or []:
        category = normalize_text(request.get("category")) or REQUEST_CATEGORY_OTHER
        for request_product in request.get("products") or []:
            product = ensure_product(
                request_product.get("name"),
                request_product.get("vendor_code"),
                request_product.get("barcode"),
            )
            amount = report_product_blocks(request_product, category)
            if category == REQUEST_CATEGORY_RECEIVING:
                product["inbound"] += amount
            elif category == REQUEST_CATEGORY_SHIPMENT:
                product["outbound"] += amount
            elif category == REQUEST_CATEGORY_DEFECT_SHIPMENT:
                product["defect_outbound"] += amount
            elif category == REQUEST_CATEGORY_RETURN:
                product["returns"] += amount

    result = list(products.values())
    result.sort(key=lambda item: normalize_text(item.get("name")).lower())
    return result


def build_skladbot_daily_report_xlsx(report: dict[str, Any]) -> tuple[bytes, str]:
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "Сводка"
    write_summary_sheet(summary_sheet, report)
    write_requests_sheet(workbook.create_sheet("Заявки"), report.get("requests") or [])
    write_request_products_sheet(workbook.create_sheet("Товары заявок"), report.get("requests") or [])
    write_movements_sheet(workbook.create_sheet("Движения"), report.get("movements") or [])
    write_stock_sheet(workbook.create_sheet("Остатки"), report)
    write_errors_sheet(workbook.create_sheet("Ошибки"), report.get("errors") or [])
    for sheet in workbook.worksheets:
        autosize_columns(sheet)
    apply_report_template_widths(workbook)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), daily_report_filename(report.get("report_date"))


def write_summary_sheet(sheet, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    report_date = format_date(report.get("report_date"))
    generated_at = report.get("generated_at")
    category_counts = summary.get("category_counts") or {}
    blocks = summary.get("request_blocks_by_category") or {}
    inbound_blocks = parse_int(blocks.get(REQUEST_CATEGORY_RECEIVING))
    outbound_blocks = parse_int(blocks.get(REQUEST_CATEGORY_SHIPMENT))
    defect_outbound_blocks = parse_int(blocks.get(REQUEST_CATEGORY_DEFECT_SHIPMENT))
    return_blocks = parse_int(blocks.get(REQUEST_CATEGORY_RETURN))
    stock_total_value = parse_int(summary.get("stock_total"))
    product_rows = product_breakdown_for_summary(report)
    if not product_rows:
        product_rows = [{
            "name": "Товар не найден",
            "ending_stock": stock_total_value,
            "inbound": 0,
            "outbound": 0,
            "defect_outbound": 0,
            "returns": 0,
        }]
    request_column = 3 + len(product_rows)
    request_column_letter = get_column_letter(request_column)
    sheet.append(["Показатель", "Значение"])
    sheet.append(["Дата отчета", report_date])
    sheet.append(["Сформировано", format_datetime(generated_at)])
    sheet.append(["customer_id", report.get("customer_id") or ""])
    sheet.append([])
    sheet.append(["Отчет о движении остатков за день"] + [None] * (request_column - 1))
    sheet.append([None, "Всего блоков"] + [item["name"] for item in product_rows] + ["Заявок"])
    sheet.append(
        ["Остаток на начало дня ", "=B13-B9-B10-B11-B12"]
        + [
            (
                f"={get_column_letter(index)}13"
                f"-{get_column_letter(index)}9"
                f"-{get_column_letter(index)}10"
                f"-{get_column_letter(index)}11"
                f"-{get_column_letter(index)}12"
            )
            for index in range(3, request_column)
        ]
        + [None]
    )
    sheet.append(
        ["Приемка", inbound_blocks]
        + [item["inbound"] for item in product_rows]
        + [category_counts.get(REQUEST_CATEGORY_RECEIVING, 0)]
    )
    sheet.append(
        ["Отгрузка", -outbound_blocks]
        + [-item["outbound"] for item in product_rows]
        + [category_counts.get(REQUEST_CATEGORY_SHIPMENT, 0)]
    )
    sheet.append(
        ["Отгрузка в браке", -defect_outbound_blocks]
        + [-item["defect_outbound"] for item in product_rows]
        + [category_counts.get(REQUEST_CATEGORY_DEFECT_SHIPMENT, 0)]
    )
    sheet.append(
        ["Возврат", return_blocks]
        + [item["returns"] for item in product_rows]
        + [category_counts.get(REQUEST_CATEGORY_RETURN, 0)]
    )
    sheet.append(
        ["Остаток на конец дня", stock_total_value]
        + [item["ending_stock"] for item in product_rows]
        + [None]
    )
    apply_header_style(sheet)
    for cell in ("A6", "A8", "B8", "A13", "B13"):
        sheet[cell].font = Font(bold=True)
    for index in range(3, request_column):
        for row in (8, 13):
            sheet.cell(row=row, column=index).font = Font(bold=True)
    apply_thin_border(sheet, f"A8:{request_column_letter}13")


def write_requests_sheet(sheet, requests: list[dict[str, Any]]) -> None:
    sheet.append(REQUEST_HEADERS)
    for request in requests:
        planned_blocks = request_blocks(request)
        actual_blocks = request_report_blocks(request)
        sheet.append([
            request.get("id") or "",
            request.get("number") or "",
            request.get("category") or "",
            request.get("type") or "",
            "Выполнена" if request.get("is_completed") else "Не выполнена",
            "Да" if request.get("archived") else "Нет",
            request.get("created_at") or "",
            request.get("updated_at") or "",
            request.get("unloading_date") or "",
            request.get("recipient") or "",
            request.get("customer_name") or "",
            request.get("address") or "",
            request.get("comment") or "",
            planned_blocks,
            actual_blocks,
            actual_blocks - planned_blocks,
            len(request.get("products") or []),
            ", ".join(request.get("include_reasons") or []),
        ])
    apply_header_style(sheet)


def write_request_products_sheet(sheet, requests: list[dict[str, Any]]) -> None:
    sheet.append(REQUEST_PRODUCT_HEADERS)
    for request in requests:
        category = normalize_text(request.get("category")) or REQUEST_CATEGORY_OTHER
        for product in request.get("products") or []:
            planned_blocks = parse_int(product.get("amount"))
            actual_blocks = report_product_blocks(product, category)
            accepted_amount = parse_int(product.get("accepted_amount"))
            sheet.append([
                request.get("number") or "",
                request.get("id") or "",
                request.get("type") or "",
                request.get("unloading_date") or "",
                request.get("recipient") or "",
                product.get("name") or "",
                product.get("vendor_code") or "",
                product.get("barcode") or "",
                planned_blocks,
                accepted_amount,
                actual_blocks,
                actual_blocks - planned_blocks,
            ])
    apply_header_style(sheet)


def write_movements_sheet(sheet, movements: list[dict[str, Any]]) -> None:
    sheet.append(MOVEMENT_HEADERS)
    for item in movements:
        sheet.append([
            item.get("direction") or "",
            item.get("date") or "",
            item.get("request_number") or "",
            item.get("movement_type") or "",
            item.get("customer") or "",
            item.get("product") or "",
            item.get("vendor_code") or "",
            item.get("barcode") or "",
            item.get("amount") or 0,
            item.get("box") or "",
            item.get("cell") or "",
        ])
    apply_header_style(sheet)


def write_stock_sheet(sheet, report: dict[str, Any]) -> None:
    rows = (report.get("stock") or {}).get("rows") or []
    summary = report.get("summary") or {}
    sheet.append(STOCK_HEADERS)
    if rows:
        for row in rows:
            sheet.append([
                row.get("customer") or "",
                row.get("product") or "",
                row.get("vendor_code") or "",
                row.get("barcode") or "",
                parse_int(row.get("stock")),
                parse_int(row.get("regular_stock")),
                parse_int(row.get("nominal_stock")),
                parse_int(row.get("available")),
            ])
    else:
        sheet.append([
            "",
            "",
            "",
            "",
            parse_int(summary.get("stock_total")),
            0,
            0,
            0,
        ])
    apply_header_style(sheet)


def write_errors_sheet(sheet, errors: list[str]) -> None:
    sheet.append(["Ошибка"])
    for error in errors:
        sheet.append([normalize_text(error)])
    apply_header_style(sheet)


def build_skladbot_daily_report_message(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    category_counts = summary.get("category_counts") or {}
    blocks = summary.get("request_blocks_by_category") or {}
    report_date = format_date(report.get("report_date"))
    lines = [
        f"SkladBot отчет за {report_date}",
        "",
        f"Заявок всего: {summary.get('requests_total', 0)}",
        f"Отгрузка: {category_counts.get(REQUEST_CATEGORY_SHIPMENT, 0)} заявок, {blocks.get(REQUEST_CATEGORY_SHIPMENT, 0)} блоков",
        f"Отгрузка в браке: {category_counts.get(REQUEST_CATEGORY_DEFECT_SHIPMENT, 0)} заявок, {blocks.get(REQUEST_CATEGORY_DEFECT_SHIPMENT, 0)} блоков",
        f"Возврат: {category_counts.get(REQUEST_CATEGORY_RETURN, 0)} заявок, {blocks.get(REQUEST_CATEGORY_RETURN, 0)} блоков",
        f"Приемка: {category_counts.get(REQUEST_CATEGORY_RECEIVING, 0)} заявок, {blocks.get(REQUEST_CATEGORY_RECEIVING, 0)} блоков",
        f"Прочее: {category_counts.get(REQUEST_CATEGORY_OTHER, 0)} заявок, {blocks.get(REQUEST_CATEGORY_OTHER, 0)} блоков",
        "",
        f"Движения: приход {summary.get('movement_in_amount', 0)}, расход {summary.get('movement_out_amount', 0)}",
        f"Актуальный остаток: {summary.get('stock_total', 0)}",
    ]
    errors = report.get("errors") or []
    if errors:
        lines.extend(["", f"Ошибки сбора: {len(errors)}. Подробности в XLSX."])
    return "\n".join(lines)


def daily_report_filename(report_date: Any) -> str:
    return f"TakSklad_SkladBot_daily_{format_date_for_filename(report_date)}.xlsx"


def format_date(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    parsed = parse_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else normalize_text(value)


def format_date_for_filename(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    parsed = parse_date(value)
    return parsed.strftime("%d.%m.%Y") if parsed else "unknown"


def format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M:%S")
    return normalize_text(value)


def apply_header_style(sheet, rows: tuple[int, ...] = (1,)) -> None:
    fill = PatternFill("solid", fgColor="1F2937")
    font = Font(color="FFFFFF", bold=True)
    for row_number in rows:
        if row_number > sheet.max_row:
            continue
        for cell in sheet[row_number]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"


def apply_thin_border(sheet, range_ref: str) -> None:
    border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )
    for row in sheet[range_ref]:
        for cell in row:
            cell.border = border


def autosize_columns(sheet) -> None:
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        width = min(60, max(10, max(len(normalize_text(cell.value)) for cell in column) + 2))
        sheet.column_dimensions[letter].width = width


def apply_report_template_widths(workbook: Workbook) -> None:
    widths_by_sheet = {
        "Сводка": {"A": 28, "B": 21, "C": 13.44, "D": 13, "E": 13, "F": 13},
        "Заявки": {"A": 10, "B": 13, "C": 11, "D": 20, "E": 11, "F": 10, "G": 15, "H": 17, "I": 15, "J": 45, "K": 33, "L": 60, "M": 13, "N": 12, "O": 12, "P": 12, "Q": 10, "R": 24},
        "Товары заявок": {"A": 13, "B": 11, "C": 20, "D": 15, "E": 45, "F": 36, "G": 17, "H": 15, "I": 12, "J": 13, "K": 12, "L": 12},
        "Движения": {"A": 13, "B": 10, "C": 17, "D": 14, "E": 10, "F": 13, "G": 13, "H": 13, "I": 13, "J": 13, "K": 13},
        "Остатки": {"A": 29, "B": 10, "C": 13, "D": 13, "E": 13, "F": 17, "G": 21, "H": 10},
        "Ошибки": {"A": 10},
    }
    for sheet_name, widths in widths_by_sheet.items():
        if sheet_name not in workbook.sheetnames:
            continue
        sheet = workbook[sheet_name]
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width
