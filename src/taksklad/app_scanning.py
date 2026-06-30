import logging
import tkinter as tk

from .backend_events import (
    load_pending_backend_events,
    remove_pending_backend_scan,
    queue_backend_scan,
    sync_pending_backend_events,
    undo_backend_scan,
)
from .backend_flow import (
    backend_blocked_scan_code,
    backend_blocked_scan_events_for_item,
    backend_event_error_message,
    backend_releases_duplicate_scan_code,
    backend_sync_item_blocker,
    format_backend_blocked_scan_message,
    order_uses_backend_scan_path,
    unsaved_backend_scan_codes,
)
from .config import BG_MAIN, FG_MUTED, STATUS_COLUMN, SUCCESS
from .desktop_scan_rules import (
    build_product_result,
    find_code_owner_in_orders,
    format_duplicate_scan_message,
    format_scan_product_mismatch_message,
    is_terminal_scan_state,
    scanned_blocks_for_order,
)
from .orders import get_order_status, get_plan_blocks
from .pending_store import (
    add_pending_save,
    is_retryable_save_error,
    update_pending_save_codes_for_undo,
    write_scan_backup,
)
from .scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    aggregate_product_mismatch,
    scan_entries_for_order_codes,
    scan_metadata_for_code,
    scan_product_mismatch,
)
from .sheets import update_scanned_codes_to_gsheet
from .utils import normalize_kiz_code, validate_kiz_code


class ScanningActionsMixin:
    def validate_code(self, code):
        is_valid, error_msg, _normalized_code = validate_kiz_code(code)
        return is_valid, error_msg

    def apply_backend_blocked_scan_events(self, blocked_events, order=None):
        order = order or self.current_order
        if not order:
            return False
        blocked_codes = [
            code for code in (backend_blocked_scan_code(item) for item in blocked_events)
            if code
        ]
        if not blocked_codes:
            return False
        blocked_set = set(blocked_codes)
        kept_codes = [
            code for code in self.scanned_codes
            if normalize_kiz_code(code) not in blocked_set
        ]
        if len(kept_codes) == len(self.scanned_codes):
            return False

        self.scanned_codes = kept_codes
        for item in blocked_events:
            code = backend_blocked_scan_code(item)
            detail = backend_event_error_message(item).lower()
            if not code:
                continue
            if "already scanned in another order item" in detail or "already scanned for another order item" in detail:
                self.all_existing_codes.add(code)
            else:
                self.all_existing_codes.discard(code)

        order["_existing_scan_entries"] = scan_entries_for_order_codes(order, self.scanned_codes)
        scanned_count = scanned_blocks_for_order(order, self.scanned_codes)
        plan_blocks = get_plan_blocks(order)
        self.safe_config(self.progress_label, text=f"{scanned_count} / {plan_blocks}")
        if scanned_count < plan_blocks:
            self.safe_config(self.next_product_btn, state="disabled")
            self.safe_config(self.finish_btn, state="disabled")
        if not write_scan_backup("backend_blocked_scan_removed", order, codes=self.scanned_codes):
            logging.warning("Backend отклонил КИЗ, но локальный backup после удаления не создан")
        self.show_error(format_backend_blocked_scan_message(blocked_events), popup=False)
        try:
            self.scan_entry.focus_set()
        except tk.TclError:
            pass
        self.update_stats_display()
        return True

    def undo_last_scan(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            self.show_error("Нет активной позиции")
            return

        if is_terminal_scan_state(self.current_order):
            self.show_error("Нельзя отменить код в архиве, возврате или закрытой смене")
            return

        if not self.scanned_codes:
            self.show_error("Нет кодов для отмены")
            return

        previous_codes = self.scanned_codes.copy()
        removed_code = self.scanned_codes.pop()
        remaining_codes = self.scanned_codes.copy()
        was_saved = len(self.scanned_codes) < self.saved_codes_count

        if not write_scan_backup("undo_scan", self.current_order, code=removed_code, codes=remaining_codes):
            self.scanned_codes.append(removed_code)
            self.show_error("Не удалось сохранить локальный backup отмены. Код не отменён")
            return

        pending_updated = update_pending_save_codes_for_undo(
            self.current_order,
            previous_codes,
            remaining_codes,
            "Откат последнего КИЗа в desktop",
        )
        if was_saved and pending_updated:
            self.saved_codes_count = len(remaining_codes)

        if was_saved and order_uses_backend_scan_path(self.current_order) and not pending_updated:
            try:
                undo_backend_scan(self.current_order, removed_code)
            except Exception as exc:
                self.scanned_codes.append(removed_code)
                self.show_error(f"Не удалось отменить код в VDS: {exc}")
                return
            self.saved_codes_count = len(remaining_codes)
        elif was_saved and not self.sheet and not pending_updated:
            self.scanned_codes.append(removed_code)
            self.show_error("Нет подключения к Google Sheets для отмены уже записанного кода")
            return

        if was_saved and self.sheet and not pending_updated and not order_uses_backend_scan_path(self.current_order):
            ok, message = update_scanned_codes_to_gsheet(
                self.sheet,
                self.current_order,
                remaining_codes,
                allow_empty=True,
            )
            if not ok:
                self.scanned_codes.append(removed_code)
                self.show_error(f"Не удалось отменить код в Google Sheets: {message}")
                return
            self.saved_codes_count = len(remaining_codes)

        self.current_order["Отсканированные коды"] = "\n".join(remaining_codes)
        self.current_order["_existing_scanned_codes"] = remaining_codes.copy()
        self.current_order["_existing_scan_entries"] = scan_entries_for_order_codes(self.current_order, remaining_codes)
        self.current_order[STATUS_COLUMN] = get_order_status(self.current_order)
        self.all_existing_codes.discard(removed_code)
        remove_pending_backend_scan(self.current_order, removed_code)

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        self.last_code_label.config(text=f"Отменён код: {removed_code[:40]}...", fg=SUCCESS)
        self.status_var.set(f"↩️ Отменён последний код ({scanned_count}/{plan_blocks})")

        if scanned_count < plan_blocks:
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="disabled")
        elif self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="normal")
        else:
            self.next_product_btn.config(state="normal")
            self.finish_btn.config(state="disabled")

        self.scan_entry.focus_set()

    def on_scan(self, event=None):
        if not self.ensure_update_allowed():
            self.scan_entry.delete(0, tk.END)
            return

        if self.operation_in_progress:
            self.show_busy_error()
            self.scan_entry.delete(0, tk.END)
            return

        if not self.current_order:
            self.show_error("Сначала выберите заказ")
            self.scan_entry.delete(0, tk.END)
            return

        is_valid, error_msg, code = validate_kiz_code(self.scan_entry.get())
        if not code:
            return

        if not is_valid:
            self.show_error(error_msg)
            self.scan_entry.delete(0, tk.END)
            return

        plan_blocks = get_plan_blocks(self.current_order)
        if plan_blocks <= 0:
            self.show_error("В заказе не указано корректное 'Кол-во блок'")
            self.scan_entry.delete(0, tk.END)
            return

        scanned_before = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        if scanned_before >= plan_blocks:
            self.show_error(f"План выполнен! Нельзя сканировать больше {plan_blocks} блоков")
            self.scan_entry.delete(0, tk.END)
            return

        scan_metadata = scan_metadata_for_code(code)
        block_quantity = scan_metadata["block_quantity"]
        product_name = self.current_order.get("Товары", "")
        if scan_product_mismatch(code, product_name):
            self.show_error(
                format_scan_product_mismatch_message(
                    code,
                    product_name,
                    scan_product_key=scan_metadata.get("product_key") or "",
                )
            )
            self.scan_entry.delete(0, tk.END)
            return
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            if aggregate_product_mismatch(code, product_name):
                self.show_error("Код короба не соответствует товару текущей позиции")
                self.scan_entry.delete(0, tk.END)
                return
            remaining_blocks = max(0, plan_blocks - scanned_before)
            if block_quantity > remaining_blocks:
                self.show_error(f"Короб +{block_quantity} блоков превышает остаток позиции: осталось {remaining_blocks}")
                self.scan_entry.delete(0, tk.END)
                return

        if code in self.scanned_codes:
            self.show_error("Код уже отсканирован в этой позиции")
            self.scan_entry.delete(0, tk.END)
            return

        if code in self.all_existing_codes:
            existing_order = find_code_owner_in_orders(code, self.today_orders)
            if not existing_order and backend_releases_duplicate_scan_code(self.current_order, code):
                self.all_existing_codes.discard(code)
                logging.info("Backend released KIZ for re-scan after return/undo/reset; ignoring stale desktop duplicate cache")
            else:
                self.show_error(format_duplicate_scan_message(code, existing_order))
                self.log_duplicate_code_async(code)
                self.scan_entry.delete(0, tk.END)
                return

        for completed in self.completed_orders:
            if code in completed.get("Коды", []):
                self.show_error("Код уже использован в другом задании сегодня")
                self.scan_entry.delete(0, tk.END)
                return

        if not write_scan_backup("scan", self.current_order, code=code, codes=self.scanned_codes + [code]):
            self.show_error("Не удалось сохранить локальный backup. Код не принят")
            self.scan_entry.delete(0, tk.END)
            return

        self.scanned_codes.append(code)
        self.all_existing_codes.add(code)
        queue_backend_scan(self.current_order, code)
        self.current_order["_existing_scan_entries"] = scan_entries_for_order_codes(self.current_order, self.scanned_codes)
        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)

        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            self.last_code_label.config(text=f"Последний код: короб +{block_quantity}: {code[:40]}...", fg=SUCCESS)
            self.status_var.set(f"✅ Отсканирован короб +{block_quantity} ({scanned_count}/{plan_blocks})")
        else:
            self.last_code_label.config(text=f"Последний код: {code[:40]}...", fg=SUCCESS)
            self.status_var.set(f"✅ Отсканирован код ({scanned_count}/{plan_blocks})")
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
        self.scan_entry.delete(0, tk.END)

        if scanned_count >= plan_blocks:
            if self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
                self.status_var.set("🎯 Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                self.next_product_btn.config(state="disabled")
                self.finish_btn.config(state="normal")
            else:
                self.status_var.set("🎯 Позиция выполнена! Нажмите 'Следующая позиция'")
                self.next_product_btn.config(state="normal")
                self.finish_btn.config(state="disabled")

        self.scan_entry.focus_set()

    def next_product(self, finish_after_save=False):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            return

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)

        if scanned_count != plan_blocks:
            self.show_error(f"Отсканировано {scanned_count} из {plan_blocks} блоков. Завершите позицию!")
            return

        order = self.current_order
        scanned_codes = self.scanned_codes.copy()
        self.set_busy("⏳ Сохраняю КИЗы в VDS..." if finish_after_save else "⏳ Сохраняю КИЗы...")
        self.safe_config(self.next_product_btn, state="disabled")
        self.safe_config(self.finish_btn, state="disabled")

        def work():
            if order_uses_backend_scan_path(order):
                for saved_code in unsaved_backend_scan_codes(order, scanned_codes):
                    queue_backend_scan(order, saved_code)
                backend_sync_result = sync_pending_backend_events()
                blocked_events = backend_blocked_scan_events_for_item(
                    backend_sync_result,
                    order.get("_backend_order_item_id"),
                )
                if blocked_events:
                    return {"backend_blocked": True, "blocked_events": blocked_events, "backend": True}
                blocker = backend_sync_item_blocker(
                    backend_sync_result,
                    order.get("_backend_order_item_id"),
                    load_pending_backend_events(),
                )
                if blocker:
                    raise RuntimeError(blocker)
                if not write_scan_backup("position_saved_backend", order, codes=scanned_codes):
                    raise RuntimeError("Коды сохранены в backend, но локальный backup позиции не создан")
                return {"queued": False, "message": "backend_saved", "backend": True}

            ok = False
            message = "Нет подключения к Google Sheets"
            if self.sheet:
                ok, message = update_scanned_codes_to_gsheet(self.sheet, order, scanned_codes)

            if not ok:
                if not is_retryable_save_error(message):
                    raise RuntimeError(message)
                add_pending_save(order, scanned_codes, message)
                if not write_scan_backup("position_queued", order, codes=scanned_codes):
                    raise RuntimeError("Google Sheets недоступен, и локальная очередь записи не создана")
                return {"queued": True, "message": message}

            if not write_scan_backup("position_saved", order, codes=scanned_codes):
                raise RuntimeError("Коды записаны в Google Sheets, но локальный backup позиции не создан")
            return {"queued": False, "message": message}

        def on_success(result):
            if result.get("backend_blocked"):
                self.clear_busy()
                if not self.apply_backend_blocked_scan_events(result.get("blocked_events") or [], order=order):
                    self.show_error(format_backend_blocked_scan_message(result.get("blocked_events") or []), popup=False)
                return

            product_result = build_product_result(order, scanned_codes, self.product_catalog)
            self.current_legal_entity_products.append(product_result)
            order["Отсканированные коды"] = "\n".join(scanned_codes)
            order[STATUS_COLUMN] = get_order_status(order)
            order["_existing_scanned_codes"] = scanned_codes.copy()
            order["_existing_scan_entries"] = scan_entries_for_order_codes(order, scanned_codes)

            completed_result = product_result.copy()
            completed_result["План блоков"] = plan_blocks
            self.completed_orders.append(completed_result)

            self.current_product_idx += 1
            self.clear_busy()

            if self.current_product_idx < len(self.current_legal_entity_orders):
                self.load_current_product()
                if result.get("queued"):
                    self.status_var.set("⚠️ Позиция сохранена локально, отправится при обновлении")
                elif result.get("backend"):
                    self.status_var.set("✅ Позиция сохранена в VDS")
                else:
                    self.status_var.set("✅ Позиция сохранена")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            else:
                self.current_order = None
                self.next_product_btn.config(state="disabled")
                if finish_after_save:
                    self.finish_btn.config(state="disabled")
                    self.status_var.set("✅ КИЗы сохранены. Готовлю завершение и печать...")
                    self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
                    self.update_stats_display()
                    self.after(0, lambda: self.finish_legal_entity(from_next_product=True))
                    return
                self.finish_btn.config(state="normal")
                if result.get("queued"):
                    self.status_var.set("⚠️ Все позиции сохранены локально. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                elif result.get("backend"):
                    self.status_var.set("✅ Все позиции сохранены в VDS. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                else:
                    self.status_var.set("✅ Все позиции сохранены. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            self.update_stats_display()

        def on_error(exc):
            self.show_critical_error("КИЗы не записаны", exc)
            self.clear_busy()
            current_plan_blocks = get_plan_blocks(self.current_order) if self.current_order else 0
            current_scanned_count = (
                scanned_blocks_for_order(self.current_order, self.scanned_codes)
                if self.current_order
                else 0
            )
            if self.current_order and current_scanned_count == current_plan_blocks:
                if self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
                    self.safe_config(self.next_product_btn, state="disabled")
                    self.safe_config(self.finish_btn, state="normal")
                else:
                    self.safe_config(self.next_product_btn, state="normal")
                    self.safe_config(self.finish_btn, state="disabled")
            else:
                self.safe_config(self.next_product_btn, state="disabled")
                self.safe_config(self.finish_btn, state="disabled")

        self.run_background(
            "Не удалось сохранить позицию",
            work,
            on_success=on_success,
            on_error=on_error
        )
