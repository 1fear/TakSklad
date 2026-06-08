import os
import time
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
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
    "Блоков",
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
    "Блоков",
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


def collect_skladbot_daily_report(report_date: date | None = None, client: Any | None = None) -> dict[str, Any]:
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
    requests = fetch_daily_requests(client, report_date, request_types, result["errors"])
    result["requests"] = requests
    result["movements"] = fetch_daily_movements(client, report_date, result["errors"])
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
    detail_limit = max(1, env_int("SKLADBOT_DAILY_REPORT_DETAIL_LIMIT", 250))
    request_delay = max(0.0, env_float("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", 0.25))
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
        except Exception as exc:
            errors.append(f"Не удалось получить список заявок type_id={type_id}: {sanitize_skladbot_error(exc)}")
            continue
        for list_item in list_items:
            if checked_details >= detail_limit:
                errors.append(f"Лимит детализации заявок достигнут: {detail_limit}")
                return result
            request_id = parse_int(request_list_value(list_item, "id"))
            if request_id <= 0 or request_id in seen_ids:
                continue
            reasons = list_request_inclusion_reasons(list_item, report_date)
            if not reasons and has_reliable_list_dates(list_item):
                continue
            try:
                detail = client.get_request_detail(request_id)
                checked_details += 1
            except Exception as exc:
                errors.append(f"Не удалось получить заявку {request_id}: {sanitize_skladbot_error(exc)}")
                continue
            if request_delay:
                time.sleep(request_delay)
            request = normalize_request_payload(list_item, detail)
            reasons = request_inclusion_reasons(request, report_date)
            if not reasons:
                continue
            request["category"] = categorize_request_type(request.get("type") or request_type.get("name"))
            request["type_id"] = type_id
            request["include_reasons"] = reasons
            seen_ids.add(request_id)
            result.append(request)
    result.sort(key=lambda item: (
        category_sort_key(item.get("category")),
        parse_int(item.get("id")),
    ))
    return result


def has_reliable_list_dates(list_item: Any) -> bool:
    if not isinstance(list_item, dict):
        return False
    return any(parse_date(request_list_value(list_item, key)) for key in (
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "unloading_date",
        "unloadingDate",
    ))


def list_request_inclusion_reasons(list_item: Any, report_date: date) -> list[str]:
    if not isinstance(list_item, dict):
        return []
    return request_inclusion_reasons({
        "created_at": request_list_value(list_item, "created_at", "createdAt"),
        "updated_at": request_list_value(list_item, "updated_at", "updatedAt"),
        "unloading_date": request_list_value(list_item, "unloading_date", "unloadingDate"),
    }, report_date)


def request_inclusion_reasons(request: dict[str, Any], report_date: date) -> list[str]:
    reasons = []
    if date_matches(request.get("created_at"), report_date):
        reasons.append("создана")
    if date_matches(request.get("updated_at"), report_date):
        reasons.append("обновлена")
    if date_matches(request.get("unloading_date"), report_date):
        reasons.append("дата выгрузки")
    return reasons


def date_matches(value: Any, expected: date) -> bool:
    parsed = parse_date(value)
    return bool(parsed and parsed == expected)


def categorize_request_type(value: Any) -> str:
    text = normalize_text(value).lower().replace("ё", "е")
    if "возврат" in text:
        return "Возврат"
    if "отгруз" in text or "расход" in text:
        return "Отгрузка"
    if "прием" in text or "приемка" in text:
        return "Приемка"
    return "Прочее"


def category_sort_key(value: Any) -> int:
    return {"Отгрузка": 1, "Возврат": 2, "Приемка": 3, "Прочее": 4}.get(normalize_text(value), 9)


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
                result.append(normalize_movement(item, direction))
    result.sort(key=lambda item: (normalize_text(item.get("date")), normalize_text(item.get("request_number"))))
    return result


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
    try:
        payload = client.post("/report/stock", {
            "customer_id": getattr(client, "customer_id", None),
            "with_details": True,
        })
    except Exception as exc:
        error = f"Не удалось получить остаток SkladBot: {sanitize_skladbot_error(exc)}"
        errors.append(error)
        return {"total": 0, "rows": [], "raw": {}, "error": error}
    rows = normalize_stock_rows(payload)
    total = stock_total(payload, rows)
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
    category_counts = {"Отгрузка": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0}
    type_counts: dict[str, int] = {}
    request_blocks_by_category = {"Отгрузка": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0}
    for request in requests:
        category = normalize_text(request.get("category")) or "Прочее"
        category_counts[category] = category_counts.get(category, 0) + 1
        type_name = normalize_text(request.get("type")) or "Без типа"
        type_counts[type_name] = type_counts.get(type_name, 0) + 1
        request_blocks_by_category[category] = request_blocks_by_category.get(category, 0) + request_blocks(request)
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


def build_skladbot_daily_report_xlsx(report: dict[str, Any]) -> tuple[bytes, str]:
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "Сводка"
    write_summary_sheet(summary_sheet, report)
    write_requests_sheet(workbook.create_sheet("Заявки"), report.get("requests") or [])
    write_request_products_sheet(workbook.create_sheet("Товары заявок"), report.get("requests") or [])
    write_movements_sheet(workbook.create_sheet("Движения"), report.get("movements") or [])
    write_stock_sheet(workbook.create_sheet("Остатки"), (report.get("stock") or {}).get("rows") or [])
    write_errors_sheet(workbook.create_sheet("Ошибки"), report.get("errors") or [])
    for sheet in workbook.worksheets:
        autosize_columns(sheet)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), daily_report_filename(report.get("report_date"))


def write_summary_sheet(sheet, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    report_date = format_date(report.get("report_date"))
    generated_at = report.get("generated_at")
    sheet.append(["Показатель", "Значение"])
    sheet.append(["Дата отчета", report_date])
    sheet.append(["Сформировано", format_datetime(generated_at)])
    sheet.append(["customer_id", report.get("customer_id") or ""])
    sheet.append(["Заявок всего", summary.get("requests_total", 0)])
    category_counts = summary.get("category_counts") or {}
    blocks = summary.get("request_blocks_by_category") or {}
    for category in ("Отгрузка", "Возврат", "Приемка", "Прочее"):
        sheet.append([f"{category}: заявок", category_counts.get(category, 0)])
        sheet.append([f"{category}: блоков в заявках", blocks.get(category, 0)])
    sheet.append(["Движений всего", summary.get("movements_total", 0)])
    sheet.append(["Приход: строк", summary.get("movement_in_rows", 0)])
    sheet.append(["Приход: количество", summary.get("movement_in_amount", 0)])
    sheet.append(["Расход: строк", summary.get("movement_out_rows", 0)])
    sheet.append(["Расход: количество", summary.get("movement_out_amount", 0)])
    sheet.append(["Актуальный остаток", summary.get("stock_total", 0)])
    sheet.append(["Строк остатков", summary.get("stock_rows", 0)])
    sheet.append(["Ошибок сбора", summary.get("errors", 0)])
    sheet.append([])
    type_header_row = sheet.max_row + 1
    sheet.append(["Тип заявки", "Количество"])
    for type_name, count in sorted((summary.get("type_counts") or {}).items()):
        sheet.append([type_name, count])
    apply_header_style(sheet, rows=(1, type_header_row))


def write_requests_sheet(sheet, requests: list[dict[str, Any]]) -> None:
    sheet.append(REQUEST_HEADERS)
    for request in requests:
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
            request_blocks(request),
            len(request.get("products") or []),
            ", ".join(request.get("include_reasons") or []),
        ])
    apply_header_style(sheet)


def write_request_products_sheet(sheet, requests: list[dict[str, Any]]) -> None:
    sheet.append(REQUEST_PRODUCT_HEADERS)
    for request in requests:
        for product in request.get("products") or []:
            sheet.append([
                request.get("number") or "",
                request.get("id") or "",
                request.get("type") or "",
                request.get("unloading_date") or "",
                request.get("recipient") or "",
                product.get("name") or "",
                product.get("vendor_code") or "",
                product.get("barcode") or "",
                product.get("amount") or 0,
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


def write_stock_sheet(sheet, rows: list[dict[str, Any]]) -> None:
    sheet.append(STOCK_HEADERS)
    for row in rows:
        sheet.append([
            row.get("customer") or "",
            row.get("product") or "",
            row.get("vendor_code") or "",
            row.get("barcode") or "",
            row.get("stock") or 0,
            row.get("regular_stock") or 0,
            row.get("nominal_stock") or 0,
            row.get("available") or 0,
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
        f"Отгрузка: {category_counts.get('Отгрузка', 0)} заявок, {blocks.get('Отгрузка', 0)} блоков",
        f"Возврат: {category_counts.get('Возврат', 0)} заявок, {blocks.get('Возврат', 0)} блоков",
        f"Приемка: {category_counts.get('Приемка', 0)} заявок, {blocks.get('Приемка', 0)} блоков",
        f"Прочее: {category_counts.get('Прочее', 0)} заявок, {blocks.get('Прочее', 0)} блоков",
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


def autosize_columns(sheet) -> None:
    for column in sheet.columns:
        letter = get_column_letter(column[0].column)
        width = min(60, max(10, max(len(normalize_text(cell.value)) for cell in column) + 2))
        sheet.column_dimensions[letter].width = width
