from datetime import datetime, timedelta

from .catalog import get_product_rule
from .config import APP_VERSION, SKLADBOT_REQUEST_NUMBER_COLUMN, STATUS_COLUMN
from .orders import get_order_date_value, get_plan_blocks
from .scan_quantities import (
    product_key_from_name,
    scan_code_product_key,
    scan_entries_for_order_codes,
    scanned_blocks_for_order_codes,
)
from .utils import (
    normalize_kiz_code,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
)


def date_sort_key(value):
    text = normalize_text(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.max


def format_order_date_header(value):
    text = parse_date_to_standard(value) or normalize_text(value) or "Без даты отгрузки"
    parsed = None
    try:
        parsed = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return text

    today = datetime.now().date()
    if parsed == today:
        prefix = "Сегодня"
    elif parsed == today + timedelta(days=1):
        prefix = "Завтра"
    elif parsed == today - timedelta(days=1):
        prefix = "Вчера"
    else:
        prefix = ""
    display = parsed.strftime("%d.%m.%Y")
    return f"{prefix}, {display}" if prefix else display


def format_money(value):
    amount = parse_int_value(value)
    if amount <= 0:
        return "сумма не указана"
    return f"{amount:,} сум".replace(",", " ")


def scanned_codes_for_order(order):
    return split_codes(order.get("Отсканированные коды") or "\n".join(order.get("_existing_scanned_codes") or []))


def scanned_blocks_for_order(order, codes=None):
    return scanned_blocks_for_order_codes(order, codes if codes is not None else scanned_codes_for_order(order))


def first_incomplete_order_index(orders):
    for index, order in enumerate(orders):
        plan_blocks = get_plan_blocks(order)
        if plan_blocks <= 0:
            return index
        if scanned_blocks_for_order(order) < plan_blocks:
            return index
    return len(orders)


def build_product_result(order, scanned_codes, product_catalog):
    pieces_per_block = get_product_rule(order.get("Товары", ""), product_catalog)["pieces_per_block"]
    plan_blocks = get_plan_blocks(order)
    return {
        "Дата отгрузки": get_order_date_value(order),
        "Клиент": order.get("Клиент", ""),
        "Адрес": order.get("Адрес", ""),
        "Торговый представитель": order.get("Торговый представитель", ""),
        "Товары": order.get("Товары", ""),
        "Тип оплаты": order.get("Тип оплаты", ""),
        "Кол-во ШТ в блоке": pieces_per_block,
        "План": plan_blocks,
        "Отсканировано": scanned_blocks_for_order(order, scanned_codes),
        "Сумма позиции": parse_int_value(order.get("Сумма позиции")),
        "Цена заказа": parse_int_value(order.get("Сумма позиции")),
        "Коды": list(scanned_codes),
    }


def group_finish_blocker(orders, completed_products):
    if not orders:
        return "Нет строк заказа для завершения"
    if len(completed_products) < len(orders):
        return "Сначала сохраните все позиции заказа"
    for idx, order in enumerate(orders, start=1):
        plan_blocks = get_plan_blocks(order)
        scanned_count = scanned_blocks_for_order(order)
        if plan_blocks <= 0:
            return f"В позиции {idx} не указано корректное 'Кол-во блок'"
        if scanned_count < plan_blocks:
            return f"Позиция {idx}: отсканировано {scanned_count} из {plan_blocks} блоков"
    return ""


def is_terminal_scan_state(order):
    status = normalize_text(order.get(STATUS_COLUMN)).lower().replace("ё", "е")
    return any(marker in status for marker in ("архив", "возврат", "закрыт", "closed", "returned", "archive"))


def format_duplicate_scan_message(code, existing_order=None):
    code = normalize_kiz_code(code)
    existing_order = existing_order if isinstance(existing_order, dict) else {}
    client = normalize_text(existing_order.get("client") or existing_order.get("Клиент"))
    order_date = normalize_text(
        existing_order.get("order_date_display")
        or existing_order.get("order_date")
        or existing_order.get("Дата отгрузки")
    )
    product = normalize_text(existing_order.get("product") or existing_order.get("Товары"))
    request_number = normalize_text(
        existing_order.get("skladbot_request_number")
        or existing_order.get("№ SkladBot")
        or existing_order.get("SkladBot")
    )
    lines = ["КИЗ уже отсканирован в другом заказе."]
    if client:
        lines.append(f"Заказ: {client}")
    if order_date:
        lines.append(f"Дата отгрузки: {order_date}")
    if product:
        lines.append(f"Товар: {product}")
    if request_number:
        lines.append(f"SkladBot: {request_number}")
    if code:
        lines.append(f"Код: {code}")
    lines.append("Сканируйте другой КИЗ.")
    return "\n".join(lines)


def find_code_owner_in_orders(code, orders):
    code = normalize_kiz_code(code)
    if not code:
        return {}
    for order in orders or []:
        order_codes = {normalize_kiz_code(value) for value in scanned_codes_for_order(order)}
        if code not in order_codes:
            continue
        return {
            "client": order.get("Клиент", ""),
            "order_date_display": get_order_date_value(order) or "",
            "product": order.get("Товары", ""),
            "skladbot_request_number": order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, ""),
        }
    return {}


PRODUCT_KEY_LABELS = {
    "brown:op": "Brown OP",
    "red:op": "RED OP",
    "gold:ssl": "Gold SSL",
    "brown:ssl": "Brown SSL",
    "red:ssl": "RED SSL",
    "green:op": "Green OP",
}


def format_product_key_label(product_key):
    key = normalize_text(product_key)
    if not key:
        return "не распознан"
    return PRODUCT_KEY_LABELS.get(key, key)


def format_scan_product_mismatch_message(code, product, expected_product_key="", scan_product_key=""):
    code = normalize_kiz_code(code)
    expected_key = normalize_text(expected_product_key) or product_key_from_name(product)
    actual_key = normalize_text(scan_product_key) or scan_code_product_key(code)
    prefix = code[:18] + ("..." if len(code) > 18 else "")
    product_text = normalize_text(product) or "товар не указан"
    return "\n".join([
        "КИЗ не соответствует товару текущей позиции.",
        f"Позиция: {product_text}",
        f"Ожидалось: {format_product_key_label(expected_key)}",
        f"КИЗ распознан как: {format_product_key_label(actual_key)}",
        f"Префикс КИЗа: {prefix}",
        f"Версия приложения: {APP_VERSION}",
        "Если SKU на блоке верный, закройте приложение и запустите обновленный TakSklad.exe.",
    ])
