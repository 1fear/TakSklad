import logging
import tkinter as tk

from PIL import Image, ImageTk

from .catalog import get_product_rule
from .config import (
    ACCENT,
    BG_CARD,
    BG_MAIN,
    BORDER,
    DISABLED_BG,
    DISABLED_FG,
    FG_MUTED,
    FG_TEXT,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    SUCCESS,
)
from .desktop_scan_rules import (
    build_product_result,
    date_sort_key,
    first_incomplete_order_index,
    format_money,
    format_order_date_header,
    scanned_blocks_for_order,
    scanned_codes_for_order,
)
from .orders import get_order_date_value, get_plan_blocks, order_group_key
from .product_images import product_image_gtin, product_image_path
from .reports import order_group_display_sort_key, unpack_order_group_key
from .scan_quantities import scan_entries_for_order_codes
from .utils import normalize_text, parse_date_to_standard, parse_int_value


PRODUCT_PHOTO_SIZE = 170
PRODUCT_PHOTO_BG = "#fffaf0"


def make_product_photo_image(product, size=PRODUCT_PHOTO_SIZE):
    path = product_image_path(product)
    if not path:
        return None
    try:
        image = Image.open(path).convert("RGBA")
        resampling = getattr(Image, "Resampling", Image)
        image.thumbnail((size, size), resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (255, 250, 240, 0))
        x = (size - image.width) // 2
        y = (size - image.height) // 2
        canvas.alpha_composite(image, (x, y))
        return ImageTk.PhotoImage(canvas)
    except Exception as exc:
        logging.warning("Не удалось загрузить фото товара %s: %s", product, exc)
        return None


def is_date_separator(value):
    return isinstance(value, tuple) and len(value) == 2 and value[0] == "__date__"


class OrderDisplayMixin:
    def refresh_legal_list(self):
        self.legal_listbox.delete(0, tk.END)
        self.visible_order_groups = []
        grouped_orders = {}
        group_dates = {}
        search_text = normalize_text(self.search_var.get()).lower() if hasattr(self, "search_var") else ""

        for order in self.today_orders:
            key = order_group_key(order)
            request_number, client, payment_type, address = unpack_order_group_key(key)
            display_request_number = request_number or "Без номера SkladBot"
            client = client or "Клиент не указан"
            payment_type = payment_type or "Оплата не указана"
            address = address or "Адрес не указан"
            search_area = " ".join([
                display_request_number,
                client,
                payment_type,
                address,
                normalize_text(order.get("Торговый представитель")),
                normalize_text(order.get("Товары")),
            ]).lower()
            if search_text and search_text not in search_area:
                continue
            grouped_orders.setdefault((request_number, client, payment_type, address), []).append(order)
            group_dates.setdefault(
                (request_number, client, payment_type, address),
                parse_date_to_standard(get_order_date_value(order)) or "Без даты",
            )

        date_groups = {}
        for key in grouped_orders:
            date_groups.setdefault(group_dates.get(key, "Без даты"), []).append(key)

        for date_value in sorted(date_groups.keys(), key=date_sort_key):
            header_index = self.legal_listbox.size()
            self.visible_order_groups.append(("__date__", date_value))
            self.legal_listbox.insert(tk.END, f"  {format_order_date_header(date_value).upper()}")
            try:
                self.legal_listbox.itemconfig(header_index, fg=FG_MUTED, bg=BG_MAIN, selectbackground=BG_MAIN)
            except tk.TclError:
                pass

            for key in sorted(date_groups[date_value], key=order_group_display_sort_key):
                request_number, client, payment_type, address = unpack_order_group_key(key)
                display_request_number = request_number or "Без номера SkladBot"
                count = len(grouped_orders[key])
                self.visible_order_groups.append(key)
                self.legal_listbox.insert(
                    tk.END,
                    f"  {display_request_number} | {client} | {payment_type} | {count} поз. | {address}",
                )
        self.update_stats_display()

    def _select_first_real_order(self):
        for index, group in enumerate(self.visible_order_groups):
            if not is_date_separator(group):
                self.legal_listbox.selection_clear(0, tk.END)
                self.legal_listbox.selection_set(index)
                self.legal_listbox.activate(index)
                return True
        return False

    def _selected_order_group(self):
        selection = self.legal_listbox.curselection()
        if not selection:
            return None
        selected_index = selection[0]
        if selected_index >= len(self.visible_order_groups):
            return None
        selected_group = self.visible_order_groups[selected_index]
        if is_date_separator(selected_group):
            self.show_error("Выберите заказ под датой, а не заголовок даты", popup=False)
            return None
        return selected_group

    def reset_current_selection(self):
        self.current_legal_entity = None
        self.current_group_key = None
        self.current_legal_entity_orders = []
        self.current_product_idx = 0
        self.current_order = None
        self.scanned_codes = []
        self.saved_codes_count = 0
        self.current_legal_entity_products = []
        self.current_info.config(text="Не выбрано")
        self.current_client_label.config(text="")
        self.current_product_label.config(text="")
        self.update_product_photo("")
        self.party_summary_label.config(text="Партия не выбрана")
        self.position_label.config(text="")
        self.progress_label.config(text="0 / 0")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self.undo_btn.config(state="disabled")
        self.last_code_label.config(text="", fg=SUCCESS)

    def select_legal_entity(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.today_orders:
            self.show_error("Нет доступных юридических лиц!")
            return

        selected_group = self._selected_order_group()
        if not selected_group:
            self.show_error("Выберите заказ из списка")
            return
        request_number, legal_entity, payment_type, address = unpack_order_group_key(selected_group)
        display_request_number = request_number or "Без номера SkladBot"

        self.current_legal_entity = legal_entity
        self.current_group_key = selected_group
        self.current_legal_entity_orders = [
            o for o in self.today_orders
            if order_group_key(o) == selected_group
        ]
        self.current_legal_entity_orders.sort(key=lambda order: parse_int_value(order.get("_row_number")))
        self.current_product_idx = first_incomplete_order_index(self.current_legal_entity_orders)
        self.scanned_codes = []
        self.current_legal_entity_products = [
            build_product_result(order, scanned_codes_for_order(order), self.product_catalog)
            for order in self.current_legal_entity_orders[:self.current_product_idx]
        ]
        self.update_party_summary_display()

        if self.current_product_idx >= len(self.current_legal_entity_orders):
            self.current_order = None
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="normal")
            self.status_var.set(f"✅ Все позиции уже сохранены: {display_request_number} | {legal_entity}")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            return

        self.load_current_product()

        self.status_var.set(f"✅ Выбран заказ: {display_request_number} | {legal_entity} | {payment_type} | {address}")
        self.scan_entry.focus_set()

    def update_party_summary_display(self):
        if not self.current_legal_entity_orders:
            self.party_summary_label.config(text="Партия не выбрана")
            return

        total_positions = len(self.current_legal_entity_orders)
        total_blocks = sum(get_plan_blocks(order) for order in self.current_legal_entity_orders)
        total_sum = sum(parse_int_value(order.get("Сумма позиции")) for order in self.current_legal_entity_orders)
        request_numbers = sorted({
            normalize_text(order.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
            for order in self.current_legal_entity_orders
            if normalize_text(order.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
        })
        shipment_dates = sorted({
            parse_date_to_standard(get_order_date_value(order)) or normalize_text(get_order_date_value(order))
            for order in self.current_legal_entity_orders
            if normalize_text(get_order_date_value(order))
        }, key=date_sort_key)

        request_text = ", ".join(request_numbers[:2]) if request_numbers else "без номера SkladBot"
        if len(request_numbers) > 2:
            request_text += f" +{len(request_numbers) - 2}"
        date_text = ", ".join(format_order_date_header(value) for value in shipment_dates) if shipment_dates else "дата не указана"

        self.party_summary_label.config(
            text=(
                f"Партия: {total_positions} поз. · {total_blocks} блок. · {format_money(total_sum)}\n"
                f"Дата отгрузки: {date_text} · Заявка: {request_text}"
            )
        )

    def load_current_product(self):
        if self.current_product_idx >= len(self.current_legal_entity_orders):
            return

        self.current_order = self.current_legal_entity_orders[self.current_product_idx]

        plan_blocks = get_plan_blocks(self.current_order)
        pieces_per_block = get_product_rule(self.current_order.get("Товары", ""), self.product_catalog)["pieces_per_block"]
        order_sum = parse_int_value(self.current_order.get("Сумма позиции"))
        order_sum_text = f"{order_sum:,} сум".replace(",", " ") if order_sum else "не указана"
        client_text = normalize_text(self.current_order.get("Клиент")) or "Юр.лицо не указано"
        product_text = normalize_text(self.current_order.get("Товары")) or "SKU не указан"

        info_text = f"""№ SkladBot: {self.current_order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, '')}
📅 Дата отгрузки: {get_order_date_value(self.current_order) or 'не указана'}
👤 Торг.пред: {self.current_order.get('Торговый представитель', '')}
📍 Адрес: {self.current_order.get('Адрес', 'Адрес не указан')}
💳 Тип оплаты: {self.current_order.get('Тип оплаты', '')}
💰 Сумма: {order_sum_text}
📦 План: {plan_blocks} блоков (1 блок = {pieces_per_block} ШТ)"""

        self.current_info.config(text=info_text)
        self.current_client_label.config(text=f"🏢 {client_text}")
        self.current_product_label.config(text=f"📦 {product_text}")
        self.update_product_photo(product_text)

        total_products = len(self.current_legal_entity_orders)
        self.position_label.config(text=f"Позиция {self.current_product_idx + 1} из {total_products}")

        existing_codes = self.current_order.get("_existing_scanned_codes", [])
        self.scanned_codes = existing_codes.copy()
        self.saved_codes_count = len(existing_codes)
        existing_entries = self.current_order.get("_existing_scan_entries") or scan_entries_for_order_codes(self.current_order, existing_codes)
        self.current_order["_existing_scan_entries"] = existing_entries
        scanned_blocks = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        self.progress_label.config(text=f"{scanned_blocks} / {plan_blocks}")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self.undo_btn.config(state="normal")
        self.scan_entry.delete(0, tk.END)
        if existing_codes:
            self.last_code_label.config(text=f"Уже записано: {scanned_blocks} блоков, {len(existing_codes)} кодов", fg=SUCCESS)
        else:
            self.last_code_label.config(text="", fg=SUCCESS)
        if plan_blocks > 0 and scanned_blocks >= plan_blocks:
            if self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
                self.next_product_btn.config(state="disabled")
                self.finish_btn.config(state="normal")
            else:
                self.next_product_btn.config(state="normal")
                self.finish_btn.config(state="disabled")
        self.scan_entry.focus_set()

    def update_product_photo(self, product_name):
        self.product_photo_canvas.delete("all")
        self.product_photo_canvas.create_rectangle(
            0,
            0,
            PRODUCT_PHOTO_SIZE,
            PRODUCT_PHOTO_SIZE,
            fill=PRODUCT_PHOTO_BG,
            outline=BORDER,
        )
        photo = make_product_photo_image(product_name)
        gtin = product_image_gtin(product_name)
        if photo is not None:
            self.product_photo_image = photo
            self.product_photo_canvas.create_image(
                PRODUCT_PHOTO_SIZE // 2,
                PRODUCT_PHOTO_SIZE // 2,
                image=self.product_photo_image,
                anchor="center",
            )
            self.product_photo_gtin_label.config(
                text=f"GTIN {gtin}" if gtin else "GTIN не указан",
                bg=FG_TEXT,
                fg="#fff7df",
            )
            self.product_photo_caption_label.config(text="Фото товара")
            return

        self.product_photo_image = None
        self.product_photo_canvas.create_text(
            PRODUCT_PHOTO_SIZE // 2,
            PRODUCT_PHOTO_SIZE // 2,
            text="Фото\nне найдено",
            fill=FG_MUTED,
            font=("Segoe UI", 12, "bold"),
            justify="center",
        )
        self.product_photo_gtin_label.config(
            text=f"GTIN {gtin}" if gtin else "SKU без фото",
            bg=DISABLED_BG,
            fg=DISABLED_FG,
        )
        self.product_photo_caption_label.config(text="Можно сканировать без фото")
