import re
import uuid
from datetime import datetime


TELEGRAM_MANUAL_CALLBACK_PREFIX = "manual:"
TELEGRAM_MANUAL_BLOCK_PRICE = 240000
TELEGRAM_MANUAL_PIECES_PER_BLOCK = 10
TELEGRAM_MANUAL_PRODUCTS = {
    "brown_op": "Chapman Brown OP 20",
    "brown_ssl": "Chapman Brown SSL 100`20",
    "red_op": "Chapman RED OP 20",
    "red_ssl": "Chapman RED SSL 100 20",
    "gold_ssl": "Chapman Gold SSL 100`20",
    "green_op": "Chapman Green OP 20",
}
TELEGRAM_MANUAL_PAYMENT_TYPES = {
    "terminal": "Терминал",
    "transfer": "Перечисление",
}
COORDINATES_PATTERN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")
DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2,4})(?!\d)")


def normalize_text(value):
    return str(value or "").strip()


def parse_int(value):
    text = normalize_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_date_from_text(value):
    match = DATE_PATTERN.search(normalize_text(value))
    if not match:
        return ""
    day, month, year = match.groups()
    if len(year) == 2:
        year = "20" + year
    try:
        return datetime.strptime(f"{int(day):02d}.{int(month):02d}.{year}", "%d.%m.%Y").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def display_date(value):
    text = normalize_text(value)
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return parse_date_from_text(text) or text


def telegram_inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}


def telegram_manual_menu_keyboard():
    return telegram_inline_keyboard([
        [{"text": "Добавить заказ вручную", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}add"}],
        [{"text": "Удалить активный заказ", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])


def telegram_manual_payment_keyboard():
    return telegram_inline_keyboard([
        [{"text": label, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}payment:{key}"}]
        for key, label in TELEGRAM_MANUAL_PAYMENT_TYPES.items()
    ])


def telegram_manual_product_keyboard():
    rows = [
        [{"text": label, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}product:{key}"}]
        for key, label in TELEGRAM_MANUAL_PRODUCTS.items()
    ]
    rows.append([{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}])
    return telegram_inline_keyboard(rows)


def telegram_manual_add_next_keyboard():
    return telegram_inline_keyboard([
        [{"text": "Добавить ещё позицию", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}add_more"}],
        [{"text": "Создать заказ", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}create"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])


def telegram_manual_delete_keyboard(orders):
    rows = []
    for index, order in enumerate(orders, start=1):
        client = normalize_text(order.get("client")) or "без клиента"
        text = f"{index}. {display_date(order.get('order_date')) or 'без даты'} | {client}"
        if len(text) > 58:
            text = text[:55] + "..."
        rows.append([{"text": text, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete:{index}"}])
    rows.append([{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}])
    return telegram_inline_keyboard(rows)


def telegram_manual_delete_confirm_keyboard(order_id):
    return telegram_inline_keyboard([
        [{"text": "Удалить из TakSklad", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete_confirm:{order_id}"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])


def manual_address_and_coordinates(value):
    text = normalize_text(value)
    match = COORDINATES_PATTERN.match(text)
    if not match:
        return text, ""
    lat, lng = match.groups()
    return "Адрес не указан", f"{lat}, {lng}"


def order_scanned_blocks(order):
    total = 0
    for item in (order or {}).get("items") or []:
        total += max(parse_int(item.get("scanned_blocks")), len(item.get("scan_codes") or []))
    return total


def order_planned_blocks(order):
    return sum(parse_int(item.get("quantity_blocks")) for item in ((order or {}).get("items") or []))


def manual_order_summary(flow):
    data = (flow or {}).get("data") or {}
    lines = [
        "Проверьте ручной заказ:",
        "",
        f"Дата отгрузки: {data.get('order_date') or ''}",
        f"Тип оплаты: {data.get('payment_type') or ''}",
        f"Клиент: {data.get('client') or ''}",
        f"Адрес: {data.get('address') or ''}",
    ]
    if data.get("coordinates"):
        lines.append(f"Координаты: {data.get('coordinates')}")
    lines.append(f"Торг.пред: {data.get('representative') or ''}")
    lines.extend(["", "Позиции:"])
    for item in data.get("items") or []:
        lines.append(f"- {item.get('product')}: {item.get('blocks')} блок.")
    return "\n".join(lines)


def build_manual_import_payload(chat_id, flow):
    data = (flow or {}).get("data") or {}
    manual_id = normalize_text(data.get("manual_id")) or str(uuid.uuid4())
    source_file = f"telegram-manual-{manual_id}.xlsx"
    rows = []
    for index, item in enumerate(data.get("items") or [], start=1):
        blocks = parse_int(item.get("blocks"))
        rows.append({
            "Дата отгрузки": data.get("order_date") or "",
            "Тип оплаты": data.get("payment_type") or "",
            "Клиент": data.get("client") or "",
            "Адрес": data.get("address") or "",
            "Координаты": data.get("coordinates") or "",
            "Торговый представитель": data.get("representative") or "",
            "Товары": item.get("product") or "",
            "Кол-во ШТ": blocks * TELEGRAM_MANUAL_PIECES_PER_BLOCK,
            "Кол-во блок": blocks,
            "Цена за блок": TELEGRAM_MANUAL_BLOCK_PRICE,
            "Сумма позиции": blocks * TELEGRAM_MANUAL_BLOCK_PRICE,
            "Источник файла": source_file,
            "ID заказа": f"telegram-manual-{manual_id}",
            "ID импорта": f"telegram-manual-{manual_id}:{index}",
        })
    return {
        "source": "telegram_manual",
        "filename": source_file,
        "telegram_chat_id": normalize_text(chat_id),
        "rows": rows,
    }
