from datetime import datetime
import tkinter as tk

from .config import ACCENT, BG_CARD, BG_MAIN, FG_MUTED, FG_TEXT, SHEET_NAME, SPREADSHEET_ID
from .orders import get_order_date_header_index, get_plan_blocks, order_group_key
from .pending_store import load_pending_prints, load_pending_saves
from .sheets import get_google_client, validate_sheet_header
from .ui_widgets import AppButton
from .utils import get_cell, normalize_payment_type, parse_date_to_standard, split_codes


def build_control_panel_stats_from_gsheet(sheet):
    all_rows = sheet.get_all_values()
    if not all_rows:
        return {}

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

    today_str = datetime.now().strftime("%d.%m.%Y")
    groups = {}
    products = {}
    payments = {"terminal": 0, "transfer": 0, "unknown": 0}
    positions = 0
    completed_positions = 0
    in_progress_positions = 0
    new_positions = 0
    plan_blocks = 0
    scanned_blocks = 0

    for row in all_rows[1:]:
        if parse_date_to_standard(get_cell(row, get_order_date_header_index(header_idx))) != today_str:
            continue

        positions += 1
        order = {col_name: get_cell(row, idx) for col_name, idx in header_idx.items()}
        group_key = order_group_key(order)
        groups.setdefault(group_key, {"positions": 0, "completed": 0})
        groups[group_key]["positions"] += 1

        blocks = get_plan_blocks(order)
        codes_count = len(split_codes(order.get("Отсканированные коды")))
        plan_blocks += blocks
        scanned_blocks += codes_count
        products[order.get("Товары", "Товар не указан")] = products.get(order.get("Товары", "Товар не указан"), 0) + blocks
        payments[normalize_payment_type(order.get("Тип оплаты"))] += 1

        if blocks > 0 and codes_count >= blocks:
            completed_positions += 1
            groups[group_key]["completed"] += 1
        elif codes_count > 0:
            in_progress_positions += 1
        else:
            new_positions += 1

    completed_groups = sum(1 for group in groups.values() if group["positions"] == group["completed"])
    active_groups = max(0, len(groups) - completed_groups)
    return {
        "positions": positions,
        "groups": len(groups),
        "active_groups": active_groups,
        "completed_groups": completed_groups,
        "completed_positions": completed_positions,
        "in_progress_positions": in_progress_positions,
        "new_positions": new_positions,
        "plan_blocks": plan_blocks,
        "scanned_blocks": scanned_blocks,
        "remaining_blocks": max(0, plan_blocks - scanned_blocks),
        "payments": payments,
        "products": dict(sorted(products.items(), key=lambda item: item[0].lower())),
        "pending_saves": len(load_pending_saves()),
        "pending_prints": len(load_pending_prints()),
    }


class ControlPanelMixin:
    def show_control_panel(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        self.set_busy("⏳ Собираю контрольную панель...")
        self.safe_config(self.control_btn, state="disabled")

        def work():
            sheet = self.sheet
            if not sheet:
                client = get_google_client()
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
            return build_control_panel_stats_from_gsheet(sheet)

        def on_success(stats):
            dialog = tk.Toplevel(self)
            dialog.title("Панель контроля")
            dialog.configure(bg=BG_MAIN)
            dialog.geometry("620x560")
            dialog.transient(self)

            container = tk.Frame(dialog, bg=BG_CARD, padx=18, pady=16)
            container.pack(fill="both", expand=True, padx=16, pady=16)

            tk.Label(container, text="Панель контроля за день", bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 12))
            rows = [
                ("Заказов по клиенту/адресу", stats.get("groups", 0)),
                ("Активных заказов", stats.get("active_groups", 0)),
                ("Завершённых заказов", stats.get("completed_groups", 0)),
                ("Позиций всего", stats.get("positions", 0)),
                ("Новые позиции", stats.get("new_positions", 0)),
                ("В работе", stats.get("in_progress_positions", 0)),
                ("Завершённые позиции", stats.get("completed_positions", 0)),
                ("План блоков", stats.get("plan_blocks", 0)),
                ("Отсканировано блоков", stats.get("scanned_blocks", 0)),
                ("Осталось блоков", stats.get("remaining_blocks", 0)),
                ("Очередь записи", stats.get("pending_saves", 0)),
                ("Очередь печати", stats.get("pending_prints", 0)),
            ]

            for label, value in rows:
                row = tk.Frame(container, bg=BG_CARD)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=f"{label}:", bg=BG_CARD, fg=FG_MUTED, width=24, anchor="w", font=("Segoe UI", 10)).pack(side="left")
                tk.Label(row, text=str(value), bg=BG_CARD, fg=FG_TEXT, anchor="w", font=("Segoe UI", 10, "bold")).pack(side="left")

            payments = stats.get("payments", {})
            tk.Label(container, text="Оплата", bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(14, 6))
            payment_text = f"Терминал: {payments.get('terminal', 0)} | Перечисление: {payments.get('transfer', 0)} | Не распознано: {payments.get('unknown', 0)}"
            tk.Label(container, text=payment_text, bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 10)).pack(anchor="w")

            tk.Label(container, text="Товары по плану", bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(14, 6))
            products_text = "\n".join([f"{name}: {blocks} блок." for name, blocks in list(stats.get("products", {}).items())[:12]])
            if not products_text:
                products_text = "Нет данных"
            tk.Label(container, text=products_text, bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 10), justify="left", wraplength=540).pack(anchor="w")

            AppButton(container, text="ЗАКРЫТЬ", bg=FG_MUTED, fg="white", font=("Segoe UI", 9, "bold"), relief="flat", command=dialog.destroy).pack(anchor="e", pady=(16, 0))

        def on_error(exc):
            self.show_critical_error("Не удалось собрать панель контроля", exc)

        def on_finally():
            self.clear_busy()
            self.safe_config(self.control_btn, state="normal")

        self.run_background(
            "Не удалось собрать панель контроля",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )
