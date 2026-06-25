from .backend_client import backend_enabled
from .backend_events import (
    load_pending_backend_events,
    queue_backend_scans_for_order,
    sync_pending_backend_events,
)
from .backend_flow import backend_sync_group_blocker, complete_backend_orders_or_raise
from .config import BG_MAIN, FG_MUTED
from .desktop_scan_rules import group_finish_blocker, scanned_blocks_for_order
from .orders import get_plan_blocks, order_group_key
from .pending_store import add_pending_print, remove_pending_print, write_scan_backup
from .printing import print_summary
from .reports import build_summary_products_from_gsheet
from .sheets import archive_order_group_to_gsheet, google_backoff_remaining
from .utils import normalize_text, parse_int_value


class FinishActionsMixin:
    def finish_legal_entity(self, from_next_product=False):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_legal_entity:
            return

        if self.current_product_idx < len(self.current_legal_entity_orders):
            if (
                not from_next_product
                and self.current_order
                and self.current_product_idx == len(self.current_legal_entity_orders) - 1
                and scanned_blocks_for_order(self.current_order, self.scanned_codes) == get_plan_blocks(self.current_order)
            ):
                self.next_product(finish_after_save=True)
                return
            self.show_error("Сначала завершите все позиции по заказу!")
            return

        if not self.current_legal_entity_products:
            self.show_error("Нет завершённых позиций по заказу!")
            return

        finish_blocker = group_finish_blocker(self.current_legal_entity_orders, self.current_legal_entity_products)
        if finish_blocker:
            self.show_error(f"Нельзя завершить заказ: {finish_blocker}")
            self.finish_btn.config(state="disabled")
            return

        group_key = self.current_group_key
        current_orders = [order.copy() for order in self.current_legal_entity_orders]
        current_products = [product.copy() for product in self.current_legal_entity_products]
        backend_order_ids = sorted({
            normalize_text(order.get("_backend_order_id"))
            for order in current_orders
            if normalize_text(order.get("_backend_order_id"))
        })
        uses_backend_finish = bool(backend_order_ids and backend_enabled())

        if self.sheet and not uses_backend_finish:
            google_pause_remaining = google_backoff_remaining()
            if google_pause_remaining > 0:
                self.show_error(
                    f"Google Sheets временно на паузе ({google_pause_remaining} сек.). "
                    "Завершение и печать запустятся после паузы."
                )
                self.finish_btn.config(state="normal")
                return

        if not self.confirm_print_settings():
            self.show_error("Печать сводного листа отменена")
            self.finish_btn.config(state="normal")
            return
        selected_print_settings = getattr(self, "_selected_print_settings", None)

        self.set_busy("⏳ Печатаю сводный лист и завершаю заказ...")
        self.safe_config(self.finish_btn, state="disabled")
        self.safe_config(self.next_product_btn, state="disabled")

        def work():
            first_product = current_products[0]
            address = first_product.get('Адрес', 'Адрес не указан')
            summary_products = current_products
            backend_complete_result = {"completed": 0, "already_completed": 0}

            if self.sheet and not uses_backend_finish:
                sheet_products = build_summary_products_from_gsheet(
                    self.sheet,
                    group_key or order_group_key(first_product)
                )
                if sheet_products:
                    summary_products = sheet_products
                    first_product = summary_products[0]
                    address = first_product.get('Адрес', address)

            pending_print_id = add_pending_print(address, summary_products)
            if not pending_print_id:
                raise RuntimeError(
                    "Не удалось поставить сводный лист в очередь печати. "
                    "Заказ не завершён в backend."
                )

            try:
                printed_files = print_summary(address, summary_products, print_settings=selected_print_settings)
                if not printed_files:
                    raise RuntimeError("Сводочный лист не создан или не отправлен на печать")
            except Exception as exc:
                raise RuntimeError(
                    f"Сводный лист не напечатался. Заказ не завершён в backend. Причина: {exc}"
                ) from exc

            if not remove_pending_print(pending_print_id):
                raise RuntimeError(
                    "Сводный лист напечатан, но очередь печати не обновилась. "
                    "Заказ не завершён в backend."
                )

            if uses_backend_finish:
                backend_sync_result = sync_pending_backend_events()
                order_item_ids = {
                    normalize_text(order.get("_backend_order_item_id"))
                    for order in current_orders
                    if normalize_text(order.get("_backend_order_item_id"))
                }
                blocker = backend_sync_group_blocker(
                    backend_sync_result,
                    order_item_ids,
                    set(backend_order_ids),
                    load_pending_backend_events(),
                )
                if blocker:
                    raise RuntimeError(
                        "Сводный лист напечатан, но backend не принял все КИЗы. "
                        f"{blocker}"
                    )
                backend_complete_result = complete_backend_orders_or_raise(backend_order_ids)

            if self.sheet and not (backend_order_ids and backend_enabled()):
                ok, archive_message = archive_order_group_to_gsheet(
                    self.sheet,
                    current_orders,
                )
                if not ok:
                    raise RuntimeError(archive_message)

            if not write_scan_backup(
                "address_finished",
                first_product,
                codes=[code for product in summary_products for code in product.get("Коды", [])]
            ):
                raise RuntimeError("Сводка напечатана, но backup завершения заказа не создан")

            if not (backend_order_ids and backend_enabled()):
                for order in current_orders:
                    queue_backend_scans_for_order(order)

            return {
                "first_product": first_product,
                "summary_products": summary_products,
                "finished_group": group_key or order_group_key(first_product),
                "finished_row_numbers": [
                    parse_int_value(order.get("_row_number"))
                    for order in current_orders
                    if parse_int_value(order.get("_row_number"))
                ],
            }

        def on_success(result):
            self.update_stats_display()

            finished_group = result["finished_group"]
            finished_row_numbers = set(result.get("finished_row_numbers") or [])
            if finished_row_numbers:
                self.today_orders = [
                    order
                    for order in self.today_orders
                    if parse_int_value(order.get("_row_number")) not in finished_row_numbers
                ]
            else:
                self.today_orders = [o for o in self.today_orders if order_group_key(o) != finished_group]
            self.refresh_legal_list()

            self.reset_current_selection()
            self.status_var.set("✅ Заказ завершён! Сводка отправлена на печать")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            self.sync_backend_events_async()

            self._select_first_real_order()

        def on_error(exc):
            self.show_critical_error("Не удалось завершить заказ", exc)
            self.safe_config(self.finish_btn, state="normal")

        def on_finally():
            self.clear_busy()

        self.run_background(
            "Не удалось завершить заказ",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally
        )
