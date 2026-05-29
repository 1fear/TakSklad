from .config import STATUS_COLUMN
from .orders import get_order_date_value
from .pending_store import load_pending_saves


def find_code_details_in_pending_saves(code):
    details = []
    for item in load_pending_saves():
        codes = item.get("codes", [])
        if code not in codes:
            continue
        order = item.get("order", {})
        details.append({
            "row_number": order.get("_row_number") or "локальная очередь",
            "date": get_order_date_value(order) or "",
            "payment": order.get("Тип оплаты", ""),
            "client": order.get("Клиент", ""),
            "address": order.get("Адрес", ""),
            "representative": order.get("Торговый представитель", ""),
            "product": order.get("Товары", ""),
            "quantity": order.get("Кол-во ШТ", ""),
            "blocks": order.get("Кол-во блок", ""),
            "status": order.get(STATUS_COLUMN, "ожидает записи"),
            "codes_count": len(codes),
        })
    return details


def format_duplicate_code_details(code, details, current_order=None):
    lines = [
        "Дублирующийся КИЗ",
        f"Код: {code}",
    ]

    if current_order:
        lines.extend([
            "",
            "Текущая попытка:",
            f"Клиент: {current_order.get('Клиент', '')}",
            f"Тип оплаты: {current_order.get('Тип оплаты', '')}",
            f"Адрес: {current_order.get('Адрес', '')}",
            f"Товар: {current_order.get('Товары', '')}",
            f"Торговый представитель: {current_order.get('Торговый представитель', '')}",
        ])

    if not details:
        lines.extend([
            "",
            "Где найден: код есть в кэше уже принятых КИЗов, но строку Google Sheets определить не удалось.",
        ])
        return "\n".join(lines)

    lines.extend(["", "Где уже занят:"])
    for detail in details[:10]:
        lines.extend([
            f"Строка Google Sheets: {detail.get('row_number')}",
            f"Дата: {detail.get('date')}",
            f"Клиент: {detail.get('client')}",
            f"Тип оплаты: {detail.get('payment')}",
            f"Адрес: {detail.get('address')}",
            f"Товар: {detail.get('product')}",
            f"Торговый представитель: {detail.get('representative')}",
            f"Кол-во ШТ: {detail.get('quantity')}",
            f"План блоков: {detail.get('blocks')}",
            f"Статус: {detail.get('status')}",
            f"Кодов в строке: {detail.get('codes_count')}",
            "",
        ])
    if len(details) > 10:
        lines.append(f"Еще совпадений: {len(details) - 10}")
    return "\n".join(lines).strip()
