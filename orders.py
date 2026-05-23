from config import (
    LEGACY_ORDER_DATE_COLUMN,
    ORDER_DATE_COLUMN,
    STATUS_COLUMN,
    STATUS_COMPLETED,
    STATUS_NOT_COMPLETED,
)
from utils import (
    get_cell,
    make_hash,
    normalize_lookup_text,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
)


def get_order_date_value(order):
    return order.get(ORDER_DATE_COLUMN) or order.get(LEGACY_ORDER_DATE_COLUMN)


def get_order_date_header_index(header_idx):
    return header_idx.get(ORDER_DATE_COLUMN, header_idx.get(LEGACY_ORDER_DATE_COLUMN))


def make_order_duplicate_key(record):
    payload = {
        "date": parse_date_to_standard(get_order_date_value(record)),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "client": normalize_lookup_text(record.get("Клиент")),
        "address": normalize_lookup_text(record.get("Адрес")),
        "representative": normalize_lookup_text(record.get("Торговый представитель")),
        "product": normalize_lookup_text(record.get("Товары")),
        "quantity": parse_int_value(record.get("Кол-во ШТ")),
    }
    if (
        not payload["date"]
        or not payload["payment"]
        or not payload["client"]
        or not payload["product"]
        or payload["quantity"] <= 0
    ):
        return ""
    return make_hash(payload)


def make_order_id(record):
    return make_hash({
        "date": parse_date_to_standard(get_order_date_value(record)),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "client": normalize_lookup_text(record.get("Клиент")),
        "address": normalize_lookup_text(record.get("Адрес")),
        "representative": normalize_lookup_text(record.get("Торговый представитель")),
        "product": normalize_lookup_text(record.get("Товары")),
        "quantity": parse_int_value(record.get("Кол-во ШТ")),
        "blocks": parse_int_value(record.get("Кол-во блок")),
    })


def get_plan_blocks(order):
    plan_blocks = parse_int_value(order.get("Кол-во блок", 0))
    if plan_blocks == 0:
        plan_blocks = parse_int_value(order.get("Кол-во блоков", 0))
    return plan_blocks


def is_order_completed(order):
    plan_blocks = get_plan_blocks(order)
    scanned_count = len(split_codes(order.get("Отсканированные коды")))
    return plan_blocks > 0 and scanned_count >= plan_blocks


def is_completed_status(value):
    status = normalize_lookup_text(value)
    if not status:
        return False
    not_completed_markers = [
        "не выполн",
        "невыполн",
        "не готов",
        "неготов",
        "нет",
        "false",
        "0",
    ]
    if any(marker in status for marker in not_completed_markers):
        return False
    completed_markers = [
        "выполн",
        "готов",
        "done",
        "complete",
        "completed",
        "yes",
        "true",
        "1",
    ]
    return any(marker in status for marker in completed_markers)


def get_order_status(order):
    return STATUS_COMPLETED if is_order_completed(order) else STATUS_NOT_COMPLETED


def is_order_active(order):
    status = normalize_text(order.get(STATUS_COLUMN))
    if status:
        return not is_completed_status(status)
    return not is_order_completed(order)


def order_group_key(order):
    client = normalize_text(order.get("Клиент")) or "Клиент не указан"
    payment_type = normalize_text(order.get("Тип оплаты")) or "Оплата не указана"
    address = normalize_text(order.get("Адрес")) or "Адрес не указан"
    return (
        client,
        payment_type,
        address,
    )


def row_matches_order(row, header_idx, order):
    order_id = normalize_text(order.get("ID заказа"))
    order_id_idx = header_idx.get("ID заказа")
    if order_id and order_id_idx is not None and get_cell(row, order_id_idx) == order_id:
        return True

    checks = [
        (
            ORDER_DATE_COLUMN,
            parse_date_to_standard(get_cell(row, get_order_date_header_index(header_idx)))
            == parse_date_to_standard(get_order_date_value(order)),
        ),
        ("Тип оплаты", get_cell(row, header_idx.get("Тип оплаты")) == normalize_text(order.get("Тип оплаты"))),
        ("Клиент", get_cell(row, header_idx.get("Клиент")) == normalize_text(order.get("Клиент"))),
        ("Адрес", get_cell(row, header_idx.get("Адрес")) == normalize_text(order.get("Адрес"))),
        ("Товары", get_cell(row, header_idx.get("Товары")) == normalize_text(order.get("Товары"))),
    ]
    return all(result for _, result in checks)
