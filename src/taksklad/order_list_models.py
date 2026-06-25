from dataclasses import dataclass

from .config import SKLADBOT_REQUEST_NUMBER_COLUMN
from .desktop_scan_rules import date_sort_key, format_order_date_header
from .orders import get_order_date_value, get_plan_blocks, order_group_key
from .reports import order_group_display_sort_key, unpack_order_group_key
from .utils import normalize_text, parse_date_to_standard


DATE_SEPARATOR_KIND = "date"
ORDER_CARD_KIND = "order"
DATE_SEPARATOR_KEY = "__date__"


@dataclass(frozen=True)
class DateSeparatorRow:
    date_value: str
    title: str
    kind: str = DATE_SEPARATOR_KIND

    @property
    def group_key(self):
        return (DATE_SEPARATOR_KEY, self.date_value)


@dataclass(frozen=True)
class OrderCardRow:
    group_key: tuple
    request_number: str
    client: str
    payment_type: str
    address: str
    shipment_date: str
    shipment_date_display: str
    sku_count: int
    blocks_count: int
    kind: str = ORDER_CARD_KIND

    @property
    def request_display(self):
        return self.request_number or "Без номера SkladBot"

    @property
    def meta_text(self):
        return f"{self.request_display} · {self.shipment_date_display}"

    @property
    def summary_text(self):
        return f"{self.sku_count} SKU · {self.blocks_count} блоков"


@dataclass(frozen=True)
class OrderListModel:
    rows: tuple
    visible_order_groups: tuple
    total_groups: int
    visible_cards: int

    @property
    def subtitle_text(self):
        return f"{self.total_groups} активных заказов · список листается вниз"

    @property
    def counter_text(self):
        return f"Показаны {self.visible_cards} из {self.total_groups}"


def is_date_separator_key(value):
    return isinstance(value, tuple) and len(value) == 2 and value[0] == DATE_SEPARATOR_KEY


def build_order_list_model(orders, search_text=""):
    search_query = normalize_text(search_text).lower()
    all_grouped_orders = {}
    visible_grouped_orders = {}
    group_dates = {}

    for order in orders or []:
        key = order_group_key(order)
        request_number, client, payment_type, address = unpack_order_group_key(key)
        all_grouped_orders.setdefault(key, []).append(order)
        group_dates.setdefault(key, _normalized_order_date(order))

        if search_query and search_query not in _order_search_area(
            order,
            request_number,
            client,
            payment_type,
            address,
        ):
            continue
        visible_grouped_orders.setdefault(key, []).append(order)

    rows = []
    visible_order_groups = []
    date_groups = {}
    for key in visible_grouped_orders:
        date_groups.setdefault(group_dates.get(key, "Без даты"), []).append(key)

    for date_value in sorted(date_groups, key=date_sort_key):
        separator = DateSeparatorRow(
            date_value=date_value,
            title=format_order_date_header(date_value).upper(),
        )
        rows.append(separator)
        visible_order_groups.append(separator.group_key)

        for key in sorted(date_groups[date_value], key=order_group_display_sort_key):
            group_orders = all_grouped_orders[key]
            row = _build_order_card_row(key, group_orders, group_dates.get(key, "Без даты"))
            rows.append(row)
            visible_order_groups.append(key)

    return OrderListModel(
        rows=tuple(rows),
        visible_order_groups=tuple(visible_order_groups),
        total_groups=len(all_grouped_orders),
        visible_cards=len(visible_grouped_orders),
    )


def _normalized_order_date(order):
    return parse_date_to_standard(get_order_date_value(order)) or "Без даты"


def _build_order_card_row(group_key, orders, date_value):
    request_number, client, payment_type, address = unpack_order_group_key(group_key)
    return OrderCardRow(
        group_key=group_key,
        request_number=request_number,
        client=client or "Клиент не указан",
        payment_type=payment_type or "Оплата не указана",
        address=address or "Адрес не указан",
        shipment_date=date_value,
        shipment_date_display=format_order_date_header(date_value),
        sku_count=len(orders),
        blocks_count=sum(get_plan_blocks(order) for order in orders),
    )


def _order_search_area(order, request_number, client, payment_type, address):
    request_number = request_number or normalize_text(order.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
    parts = [
        request_number or "Без номера SkladBot",
        client or "Клиент не указан",
        payment_type or "Оплата не указана",
        address or "Адрес не указан",
        normalize_text(order.get("Торговый представитель")),
        normalize_text(order.get("Товары")),
    ]
    return " ".join(parts).lower()
