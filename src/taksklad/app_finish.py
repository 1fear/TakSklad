import logging
import time

from .backend_events import (
    load_pending_backend_events,
    sync_pending_backend_events,
)
from .backend_flow import (
    backend_blocker_is_offline,
    backend_group_blocker_error,
    backend_sync_group_blocker,
    complete_backend_orders_or_raise,
    queue_backend_orders_complete,
)
from .config import BG_MAIN, FG_MUTED
from .desktop_scan_rules import (
    drop_finishing_group,
    forget_sheet_printed,
    get_finishing_groups,
    group_finish_blocker,
    mark_finishing_answered,
    mark_sheet_printed,
    remember_finishing_hidden_orders,
    scanned_blocks_for_order,
    sheet_already_printed,
)
from .orders import get_plan_blocks, order_group_key
from .pending_store import add_pending_print, remove_pending_print, write_scan_backup
from .printing import print_summary
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

        if self.current_group_key and self.current_group_key in get_finishing_groups(self):
            self.show_busy_error()
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
        if not backend_order_ids or any(
            not normalize_text(order.get("_backend_order_item_id"))
            for order in current_orders
        ):
            self.show_error("Заказ не связан с backend. Завершение заблокировано")
            self.finish_btn.config(state="normal")
            return

        first_product = current_products[0]
        summary_products = current_products
        finished_group = group_key or order_group_key(first_product)

        # Лист по этой группе уже вышел, а сервер завершение не подтвердил:
        # повторное «Завершить» печатает второй лист только по ответу «Да»
        reprint_sheet = not sheet_already_printed(self, finished_group) or self.confirm_reprint_summary()
        if reprint_sheet and not self.confirm_print_settings():
            self.show_error("Печать сводного листа отменена")
            self.finish_btn.config(state="normal")
            return
        selected_print_settings = getattr(self, "_selected_print_settings", None)

        finished_row_numbers = {
            parse_int_value(order.get("_row_number"))
            for order in current_orders
            if parse_int_value(order.get("_row_number"))
        }
        order_item_ids = {
            normalize_text(order.get("_backend_order_item_id"))
            for order in current_orders
            if normalize_text(order.get("_backend_order_item_id"))
        }
        group_order_ids = set(backend_order_ids)
        failure_title = "Не удалось завершить заказ"
        # Для лога: номер заявки SkladBot (первая часть ключа группы), без идентификаторов заказов backend
        log_request = normalize_text(finished_group[0]) if isinstance(finished_group, (tuple, list)) and finished_group else ""
        log_label = f"заявка {log_request or 'без номера'}, позиций {len(current_orders)}"
        # Заказы группы, снятые со списка на время завершения: вернутся, если сервер откажет
        hidden_orders = []
        # Ставится, когда сервер уже закрыл заказ: дальше группу в список возвращать нельзя
        server_answered = {"value": False}

        def print_work():
            address = first_product.get('Адрес', 'Адрес не указан')

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

        def complete_work():
            started_at = time.monotonic()
            logging.info("Завершение заказа в фоне: начало, %s", log_label)
            try:
                backend_sync_result = sync_pending_backend_events(
                    order_item_ids=order_item_ids,
                    order_ids=group_order_ids,
                )
                blocker = backend_sync_group_blocker(
                    backend_sync_result,
                    order_item_ids,
                    group_order_ids,
                    load_pending_backend_events(),
                )
                if blocker:
                    if backend_blocker_is_offline(blocker):
                        # Текст оператору обещает, что события заказа уйдут сами:
                        # завершение заказа встаёт в очередь вслед за его сканами
                        queue_backend_orders_complete(backend_order_ids)
                    raise backend_group_blocker_error(blocker)
                complete_backend_orders_or_raise(backend_order_ids)
                server_answered["value"] = True

                if not write_scan_backup(
                    "address_finished",
                    first_product,
                    codes=[code for product in summary_products for code in product.get("Коды", [])]
                ):
                    raise RuntimeError("Сводка напечатана, но backup завершения заказа не создан")
            except Exception:
                logging.info(
                    "Завершение заказа в фоне: ошибка, %s, %.1f сек.",
                    log_label,
                    time.monotonic() - started_at,
                )
                raise
            logging.info(
                "Завершение заказа в фоне: успех, %s, %.1f сек.",
                log_label,
                time.monotonic() - started_at,
            )

        def on_completed(_result):
            # Группа остаётся скрытой, пока не применится обновление списка, начатое после ответа сервера:
            # обновление, начатое раньше, могло получить заказ ещё не завершённым
            mark_finishing_answered(self, finished_group)
            forget_sheet_printed(self, finished_group)
            self.refresh_legal_list()

            self.status_var.set("✅ Заказ завершён! Сводка отправлена на печать")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            self.sync_backend_events_async()

        def on_completion_error(exc):
            if server_answered["value"]:
                # Сервер заказ уже закрыл, сбой был после: возврат группы в список дал бы второй сводный лист
                mark_finishing_answered(self, finished_group)
                forget_sheet_printed(self, finished_group)
            else:
                # Отметка о напечатанном листе остаётся: повторное «Завершить» спросит про печать
                restored_orders = list(hidden_orders)
                drop_finishing_group(self, finished_group)
                # Множество «работающих» опустело, но окно, которое ждёт завершения, закрывать нельзя:
                # оператор должен увидеть ошибку и вернувшийся заказ
                self.finishing_failed_before_answer = True
                self.today_orders = list(self.today_orders) + restored_orders
            try:
                self.refresh_legal_list()
            finally:
                self.show_critical_error(failure_title, exc)

        def start_completion():
            # Экран не занят: оператор может выбирать и сканировать следующий заказ
            self.run_background(
                failure_title,
                complete_work,
                on_success=on_completed,
                on_error=on_completion_error,
            )

        def on_printed(_result):
            # Лист в руках: группа скрыта от списка до ответа сервера, экран свободен
            try:
                mark_sheet_printed(self, finished_group)
                get_finishing_groups(self).add(finished_group)
                kept_orders = []
                removed_orders = []
                for order in self.today_orders:
                    if finished_row_numbers:
                        finished = parse_int_value(order.get("_row_number")) in finished_row_numbers
                    else:
                        finished = order_group_key(order) == finished_group
                    (removed_orders if finished else kept_orders).append(order)
                self.today_orders = kept_orders
                hidden_orders.extend(removed_orders)
                remember_finishing_hidden_orders(self, finished_group, hidden_orders)
                self.clear_busy()
                self.reset_current_selection()
                self.refresh_legal_list()
                self.status_var.set("✅ Сводный лист напечатан, завершаю заказ в фоне")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
                self._select_first_real_order()
            finally:
                start_completion()

        def on_print_error(exc):
            self.show_critical_error(failure_title, exc)
            self.safe_config(self.finish_btn, state="normal")

        def on_print_finally():
            self.clear_busy()

        if not reprint_sheet:
            # Оператор ответил «Нет»: лист уже в руках, сразу стадия завершения на сервере
            self.safe_config(self.finish_btn, state="disabled")
            self.safe_config(self.next_product_btn, state="disabled")
            on_printed(None)
            return

        self.set_busy("⏳ Печатаю сводный лист и завершаю заказ...")
        self.safe_config(self.finish_btn, state="disabled")
        self.safe_config(self.next_product_btn, state="disabled")
        self.run_background(
            failure_title,
            print_work,
            on_success=on_printed,
            on_error=on_print_error,
            on_finally=on_print_finally
        )
