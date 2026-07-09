import tkinter as tk

from .backend_client import (
    backend_read_orders_enabled,
    fetch_returned_orders,
    lookup_return_order,
    mark_order_returned,
)
from .config import ACCENT, BG_CARD, BG_MAIN, BORDER, FG_MUTED, FG_TEXT
from .sheets import (
    fetch_returned_orders_from_gsheet,
    lookup_return_order_in_gsheet,
    mark_return_order_in_gsheet,
)
from .ui_widgets import AppButton
from .utils import normalize_text, parse_int_value


RETURN_CONFIRMED_ITEM_KEYS = ("item_id", "product", "sku", "quantity_blocks", "quantity_pieces")
PARTIAL_RETURN_UNSUPPORTED_MESSAGE = (
    "Частичный возврат пока не поддержан для этого пути. "
    "Выберите все позиции или проведите возврат вручную через backend/SkladBot."
)
EMPTY_RETURN_SELECTION_MESSAGE = "Возврат не сохранён: выберите хотя бы одну позицию."
ALREADY_RETURNED_MESSAGE = "Этот возврат уже принят."


def return_item_blocks(item):
    return parse_int_value(item.get("quantity_blocks") or item.get("Кол-во блок"))


def return_item_line_total(item):
    return parse_int_value(item.get("line_total") or item.get("Сумма") or item.get("Сумма заказа"))


def return_order_total_blocks(order):
    return sum(return_item_blocks(item) for item in order.get("items") or [])


def return_order_total_price(order):
    return sum(return_item_line_total(item) for item in order.get("items") or [])


def return_order_already_returned(order):
    return (
        normalize_text(order.get("status")).lower() == "returned"
        or normalize_text(order.get("return_status")).lower() == "returned"
    )


def sanitize_return_confirmed_item(item):
    item = item if isinstance(item, dict) else {}
    item_id = normalize_text(item.get("item_id") or item.get("order_item_id") or item.get("id"))
    product = normalize_text(item.get("product") or item.get("sku"))
    sku = normalize_text(item.get("sku") or product)
    quantity_blocks = parse_int_value(item.get("quantity_blocks"))
    quantity_pieces = parse_int_value(item.get("quantity_pieces"))
    if not item_id or not product or quantity_blocks <= 0:
        return None
    return {
        "item_id": item_id,
        "product": product,
        "sku": sku or product,
        "quantity_blocks": quantity_blocks,
        "quantity_pieces": quantity_pieces,
    }


def build_return_confirmed_items_for_order(order, selected_item_ids=None):
    confirmed = []
    selected = None
    if selected_item_ids is not None:
        selected = {normalize_text(item_id) for item_id in selected_item_ids if normalize_text(item_id)}
    is_google_order = normalize_text(order.get("source")) == "google_sheets" or order.get("_row_numbers")
    for index, item in enumerate(order.get("items") or [], start=1):
        item_id = normalize_text(item.get("id") or item.get("item_id") or item.get("order_item_id") or item.get("_backend_order_item_id"))
        if not item_id and is_google_order and not normalize_text(order.get("_backend_order_id")):
            item_id = f"google_row:{index}"
        if selected is not None and item_id not in selected:
            continue
        product = normalize_text(item.get("product") or item.get("sku") or item.get("Товары"))
        quantity_blocks = parse_int_value(item.get("quantity_blocks") or item.get("Кол-во блок"))
        quantity_pieces = parse_int_value(item.get("quantity_pieces") or item.get("Кол-во ШТ"))
        sanitized = sanitize_return_confirmed_item({
            "item_id": item_id,
            "product": product,
            "sku": product,
            "quantity_blocks": quantity_blocks,
            "quantity_pieces": quantity_pieces,
        })
        if sanitized:
            confirmed.append(sanitized)
    return confirmed


def return_partial_supported(order):
    return False


def selected_return_is_partial(order, confirmed_items):
    all_items = build_return_confirmed_items_for_order(order)
    if not all_items:
        return False
    all_ids = {item["item_id"] for item in all_items}
    selected_ids = {item["item_id"] for item in confirmed_items or []}
    if not selected_ids:
        return False
    if not selected_ids.issubset(all_ids):
        raise RuntimeError("Состав возврата содержит позицию не из этого заказа.")
    return selected_ids != all_ids


class ReturnsActionsMixin:
    def show_returns_window(self):
        dialog = tk.Toplevel(self)
        dialog.title("Возвраты TakSklad")
        dialog.geometry("640x560")
        dialog.configure(bg=BG_MAIN)
        dialog.transient(self)

        container = tk.Frame(dialog, bg=BG_CARD, padx=20, pady=18)
        container.pack(fill="both", expand=True, padx=18, pady=18)

        tk.Label(
            container,
            text="ВОЗВРАТЫ",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            container,
            text="Сканируйте ШК накладной или введите номер/ID заявки SkladBot.",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 14))

        lookup_var = tk.StringVar()
        lookup_row = tk.Frame(container, bg=BG_CARD)
        lookup_row.pack(fill="x", pady=(0, 12))

        lookup_entry = tk.Entry(
            lookup_row,
            textvariable=lookup_var,
            bg=BG_MAIN,
            fg=FG_TEXT,
            font=("Segoe UI", 14),
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            insertbackground=FG_TEXT,
        )
        lookup_entry.pack(side="left", fill="x", expand=True)

        lookup_btn = AppButton(
            lookup_row,
            text="НАЙТИ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        lookup_btn.pack(side="right", padx=(8, 0))

        result_var = tk.StringVar(value="Заказ не выбран")
        result_label = tk.Label(
            container,
            textvariable=result_var,
            bg=BG_CARD,
            fg=FG_TEXT,
            justify="left",
            anchor="nw",
            wraplength=500,
            font=("Segoe UI", 10),
        )
        result_label.pack(fill="both", expand=True, pady=(0, 12))

        tk.Label(
            container,
            text="ПОСЛЕДНИЕ ВОЗВРАТЫ",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        returns_list = tk.Listbox(
            container,
            height=6,
            bg=BG_MAIN,
            fg=FG_TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Segoe UI", 9),
        )
        returns_list.pack(fill="x", pady=(0, 12))

        actions = tk.Frame(container, bg=BG_CARD)
        actions.pack(fill="x")

        return_btn = AppButton(
            actions,
            text="ПРИНЯТЬ ВОЗВРАТ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            state="disabled",
        )
        return_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def show_order(order):
            self.return_lookup_result = order
            total_blocks = return_order_total_blocks(order)
            total_price = return_order_total_price(order)
            already_returned = return_order_already_returned(order)
            returned_at = normalize_text(order.get("returned_at"))
            return_reference = normalize_text(order.get("return_reference"))
            lines = [
                f"Заявка: {order.get('skladbot_request_number') or order.get('skladbot_request_id') or 'без номера'}",
                f"Дата отгрузки: {order.get('order_date') or ''}",
                f"Клиент: {order.get('client') or ''}",
                f"Оплата: {order.get('payment_type') or ''}",
                f"Адрес: {order.get('address') or ''}",
                f"Позиций: {len(order.get('items') or [])}",
                f"Блоков: {total_blocks}",
                f"Сумма заказа: {total_price:,} сум".replace(",", " "),
            ]
            sku_lines = [
                f"- {item.get('product') or item.get('Товары') or 'SKU не указан'}: {return_item_blocks(item)} блок."
                for item in order.get("items") or []
            ]
            if sku_lines:
                lines.extend(["", "Состав возврата:", *sku_lines])
            if already_returned:
                lines.extend([
                    "",
                    ALREADY_RETURNED_MESSAGE,
                    f"Дата возврата: {returned_at[:19] if returned_at else 'не указана'}",
                    f"Основание: {return_reference or 'не указано'}",
                ])
            result_var.set(
                "\n".join(lines),
            )
            return_btn.config(state="disabled" if already_returned else "normal")

        def return_list_line(order):
            returned_at = normalize_text(order.get("returned_at"))
            returned_date = returned_at[:10] if returned_at else ""
            request_number = order.get("skladbot_request_number") or order.get("skladbot_request_id") or "без номера"
            total_blocks = return_order_total_blocks(order)
            return " | ".join([
                returned_date or "без даты",
                request_number,
                order.get("client") or "клиент не указан",
                f"{total_blocks} блок.",
            ])

        def refresh_returns_list():
            returns_list.delete(0, tk.END)
            returns_list.insert(tk.END, "Загружаю возвраты...")

            def on_success(orders):
                returns_list.delete(0, tk.END)
                orders = orders if isinstance(orders, list) else []
                if not orders:
                    returns_list.insert(tk.END, "Возвратов пока нет")
                    return
                for order in orders[:50]:
                    returns_list.insert(tk.END, return_list_line(order))

            def on_error(exc):
                returns_list.delete(0, tk.END)
                returns_list.insert(tk.END, f"Не удалось загрузить возвраты: {exc}")

            self.run_background(
                "Не удалось загрузить список возвратов",
                lambda: self.fetch_returns_for_display(limit=50),
                on_success=on_success,
                on_error=on_error,
            )

        def do_lookup(_event=None):
            lookup = normalize_text(lookup_var.get())
            if not lookup:
                result_var.set("Введите или отсканируйте номер заявки.")
                return
            return_btn.config(state="disabled")
            result_var.set("Ищу закрытую заявку в архиве...")

            def on_success(order):
                show_order(order)

            def on_error(exc):
                self.return_lookup_result = None
                result_var.set(f"Не найдено: {exc}")

            self.run_background(
                "Не удалось найти заявку для возврата",
                lambda: self.lookup_return_for_display(lookup),
                on_success=on_success,
                on_error=on_error,
            )

        lookup_btn.config(command=do_lookup)

        def do_return():
            order = self.return_lookup_result
            if not order:
                result_var.set("Сначала найдите заявку.")
                return
            confirmed_items = self.build_return_confirmed_items(order)
            if not confirmed_items:
                result_var.set("Возврат не сохранён: в заказе нет состава для подтверждения.")
                return
            selected_items = self.show_return_confirmation_dialog(order, confirmed_items)
            if selected_items is None:
                return
            if not selected_items:
                result_var.set(EMPTY_RETURN_SELECTION_MESSAGE)
                return
            return_btn.config(state="disabled")
            result_var.set("Фиксирую возврат...")

            def on_success(updated_order):
                storage_name = "Google Sheets" if normalize_text(updated_order.get("source")) == "google_sheets" else "backend"
                return_request = updated_order.get("skladbot_return_request_number") or updated_order.get("skladbot_return_request_id") or "создается в фоне"
                result_var.set(
                    "Возврат принят.\n\n"
                    f"Заявка: {updated_order.get('skladbot_request_number') or updated_order.get('id')}\n"
                    f"Возврат SkladBot: {return_request}\n"
                    f"Статус сохранён в {storage_name}."
                )
                refresh_returns_list()
                self.refresh_from_sheet()

            def on_error(exc):
                result_var.set(f"Возврат не сохранён: {exc}")
                return_btn.config(state="normal")

            self.run_background(
                "Не удалось принять возврат",
                lambda: self.mark_return_for_display(order, normalize_text(lookup_var.get()), confirmed_items=selected_items),
                on_success=on_success,
                on_error=on_error,
            )

        return_btn.config(command=do_return)
        AppButton(
            actions,
            text="ЗАКРЫТЬ",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=dialog.destroy,
        ).pack(side="right", fill="x", expand=True)

        lookup_entry.bind("<Return>", do_lookup)
        lookup_entry.focus_set()
        refresh_returns_list()


    def build_return_confirmed_items(self, order, selected_item_ids=None):
        return build_return_confirmed_items_for_order(order, selected_item_ids=selected_item_ids)


    def show_return_confirmation_dialog(self, order, confirmed_items):
        dialog = tk.Toplevel(self)
        dialog.title("Подтвердить возврат")
        dialog.geometry("620x500")
        dialog.configure(bg=BG_MAIN)
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_CARD, padx=18, pady=16)
        container.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(
            container,
            text="ПОДТВЕРЖДЕНИЕ ВОЗВРАТА",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")

        details = [
            f"Исходная заявка: {order.get('skladbot_request_number') or order.get('skladbot_request_id') or 'без номера'}",
            f"Дата отгрузки: {order.get('order_date') or ''}",
            f"Юр.лицо: {order.get('client') or ''}",
            f"Тип оплаты: {order.get('payment_type') or ''}",
            f"Адрес: {order.get('address') or ''}",
        ]
        tk.Label(
            container,
            text="\n".join(details),
            bg=BG_CARD,
            fg=FG_TEXT,
            justify="left",
            anchor="w",
            wraplength=560,
            font=("Segoe UI", 10),
        ).pack(fill="x", pady=(10, 12))

        tk.Label(
            container,
            text="СОСТАВ К ВОЗВРАТУ",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        items_list = tk.Listbox(
            container,
            height=min(10, max(4, len(confirmed_items))),
            bg=BG_MAIN,
            fg=FG_TEXT,
            selectmode=tk.MULTIPLE,
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Segoe UI", 10),
        )
        items_list.pack(fill="both", expand=True)
        for item in confirmed_items:
            items_list.insert(
                tk.END,
                f"{item.get('product')}: {parse_int_value(item.get('quantity_blocks'))} блок.",
            )
        items_list.selection_set(0, tk.END)

        status_var = tk.StringVar(value="Выбран полный возврат.")
        tk.Label(
            container,
            textvariable=status_var,
            bg=BG_CARD,
            fg=FG_MUTED,
            justify="left",
            anchor="w",
            wraplength=560,
            font=("Segoe UI", 9),
        ).pack(fill="x", pady=(8, 0))

        result = {"confirmed_items": None}
        actions = tk.Frame(container, bg=BG_CARD)
        actions.pack(fill="x", pady=(14, 0))

        def confirm():
            selected_indices = list(items_list.curselection())
            if not selected_indices:
                status_var.set(EMPTY_RETURN_SELECTION_MESSAGE)
                return
            selected_items = [confirmed_items[index] for index in selected_indices]
            if len(selected_items) != len(confirmed_items) and not return_partial_supported(order):
                status_var.set(PARTIAL_RETURN_UNSUPPORTED_MESSAGE)
                return
            result["confirmed_items"] = selected_items
            dialog.destroy()

        AppButton(
            actions,
            text="ПОДТВЕРДИТЬ ВОЗВРАТ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=confirm,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        AppButton(
            actions,
            text="ОТМЕНА",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=dialog.destroy,
        ).pack(side="right", fill="x", expand=True)

        dialog.wait_window()
        return result["confirmed_items"]


    def fetch_returns_for_display(self, limit=50):
        if backend_read_orders_enabled():
            return fetch_returned_orders(limit=limit)
        return fetch_returned_orders_from_gsheet(limit=limit)


    def lookup_return_for_display(self, lookup):
        if backend_read_orders_enabled():
            return lookup_return_order(lookup)
        return lookup_return_order_in_gsheet(lookup)


    def mark_return_for_display(self, order, return_reference, confirmed_items=None):
        is_google_order = normalize_text(order.get("source")) == "google_sheets" or order.get("_row_numbers")
        backend_order_id = normalize_text(order.get("_backend_order_id"))
        if not is_google_order:
            backend_order_id = normalize_text(order.get("id") or backend_order_id)
        backend_reads_enabled = backend_read_orders_enabled()
        if is_google_order and backend_reads_enabled and not backend_order_id:
            raise RuntimeError("Возврат нужно провести через backend/order id: у Google-заявки нет _backend_order_id.")

        if confirmed_items is None:
            payload_items = build_return_confirmed_items_for_order(order)
        else:
            payload_items = []
            for item in confirmed_items:
                sanitized = sanitize_return_confirmed_item(item)
                if sanitized:
                    payload_items.append(sanitized)
        if confirmed_items is not None and not payload_items:
            raise RuntimeError(EMPTY_RETURN_SELECTION_MESSAGE)
        if selected_return_is_partial(order, payload_items) and not return_partial_supported(order):
            raise RuntimeError(PARTIAL_RETURN_UNSUPPORTED_MESSAGE)

        if backend_order_id and backend_reads_enabled:
            return mark_order_returned(
                backend_order_id,
                return_reference=return_reference,
                returned_by=self.telegram_lock_owner_label,
                confirmed_items=payload_items,
            )

        if is_google_order:
            updated_order = mark_return_order_in_gsheet(
                order,
                return_reference=return_reference,
                returned_by=self.telegram_lock_owner_label,
            )
            return updated_order

        return mark_order_returned(
            order.get("id"),
            return_reference=return_reference,
            returned_by=self.telegram_lock_owner_label,
            confirmed_items=payload_items,
        )
