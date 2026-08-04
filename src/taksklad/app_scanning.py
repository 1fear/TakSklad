import logging
import socket
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
    backend_duplicate_scan_reuse_status,
    backend_event_error_message,
    backend_sync_item_blocker,
    format_backend_blocked_scan_message,
    order_uses_backend_scan_path,
    unsaved_backend_scan_codes,
)
from .config import BG_CARD, BG_MAIN, BORDER, DANGER, FG_MUTED, FG_TEXT, STATUS_COLUMN, SUCCESS
from .kiz_blocklist import blocked_kiz_reason
from .desktop_scan_rules import (
    build_product_result,
    find_code_owner_in_orders,
    format_duplicate_scan_message,
    format_scan_product_mismatch_message,
    is_terminal_scan_state,
    scan_sku_guard_status,
    scanned_blocks_for_order,
)
from .orders import get_order_status, get_plan_blocks
from .pending_store import write_scan_backup
from .scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    aggregate_product_mismatch,
    scan_entries_for_order_codes,
    scan_metadata_for_code,
    scan_product_mismatch,
)
from .backend_client import release_kiz as release_backend_kiz
from .ui_widgets import AppButton
from .utils import normalize_kiz_code, normalize_text, validate_kiz_code


# Operator-facing reasons, mirrored from backend/app/orders_service.py::KIZ_RELEASE_REASONS.
KIZ_RELEASE_REASONS = (
    ("returned_to_shelf", "Блок вернули на полку, он не уезжал"),
    ("picked_by_mistake", "Отсканирован по ошибке при сборке"),
    ("order_rebuilt", "Заказ пересобрали"),
    ("scanner_glitch", "Ошибка сканера"),
)


class ScanningActionsMixin:
    def set_scan_entry_enabled(self, enabled, message=""):
        if hasattr(self, "scan_entry"):
            try:
                self.scan_entry.config(state="normal")
                if not enabled:
                    self.scan_entry.delete(0, tk.END)
                    self.scan_entry.config(state="disabled")
            except tk.TclError:
                pass
        if hasattr(self, "scan_guard_label"):
            status = scan_sku_guard_status(self.current_order if enabled else None)
            self.safe_config(
                self.scan_guard_label,
                text=message or status.get("message") or "",
                fg=SUCCESS if enabled and status.get("state") == "active" else FG_MUTED,
            )

    def update_scan_guard_status(self):
        status = scan_sku_guard_status(self.current_order)
        if hasattr(self, "scan_guard_label"):
            self.safe_config(
                self.scan_guard_label,
                text=status.get("message") or "",
                fg=SUCCESS if status.get("state") == "active" else FG_MUTED,
            )
        return status

    def clear_scan_entry_value(self):
        if not hasattr(self, "scan_entry"):
            return
        try:
            self.scan_entry.delete(0, tk.END)
        except tk.TclError:
            try:
                self.scan_entry.config(state="normal")
                self.scan_entry.delete(0, tk.END)
                self.scan_entry.config(state="disabled")
            except tk.TclError:
                pass

    def focus_scan_entry(self):
        if not hasattr(self, "scan_entry"):
            return
        try:
            self.scan_entry.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def play_scan_feedback_sound(self, accepted):
        if accepted:
            return False
        bell = getattr(self, "bell", None)
        if not callable(bell):
            return False
        try:
            bell()
            return True
        except tk.TclError:
            return False

    def set_scan_feedback(self, state, message):
        self.scan_feedback_state = state
        self.last_scan_feedback_message = message

    def reject_scan(self, message, *, popup=True, focus=True):
        ScanningActionsMixin.set_scan_feedback(self, "rejected", message)
        self.show_error(message, popup=popup)
        ScanningActionsMixin.play_scan_feedback_sound(self, accepted=False)
        ScanningActionsMixin.clear_scan_entry_value(self)
        if focus:
            ScanningActionsMixin.focus_scan_entry(self)

    def accept_scan(self, message):
        ScanningActionsMixin.set_scan_feedback(self, "accepted", message)
        ScanningActionsMixin.play_scan_feedback_sound(self, accepted=True)

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
        ScanningActionsMixin.focus_scan_entry(self)
        self.update_stats_display()
        return True

    def undo_last_scan(self):
        return ScanningActionsMixin.undo_scan_at_index(self, len(self.scanned_codes) - 1)

    def prompt_kiz_release(self, code, message):
        """Offer to free a busy KIZ when the operator has the block in hand."""
        if not order_uses_backend_scan_path(self.current_order):
            return
        if getattr(self, "tk", None) is None:
            return

        try:
            dialog = tk.Toplevel(self)
        except tk.TclError:
            return
        dialog.title("КИЗ занят")
        dialog.configure(bg=BG_MAIN)
        dialog.geometry("700x460")
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_MAIN, padx=16, pady=16)
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=message,
            bg=BG_MAIN,
            fg=DANGER,
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=650,
        ).pack(fill="x", pady=(0, 10))

        tk.Label(
            container,
            text="Блок физически у вас на складе? Укажите причину и освободите КИЗ",
            bg=BG_MAIN,
            fg=FG_TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        card = tk.Frame(container, bg=BG_CARD, bd=1, highlightbackground=BORDER)
        card.pack(fill="x")
        reason_var = tk.StringVar(value=KIZ_RELEASE_REASONS[0][0])
        for value, label in KIZ_RELEASE_REASONS:
            tk.Radiobutton(
                card,
                text=label,
                value=value,
                variable=reason_var,
                bg=BG_CARD,
                fg=FG_TEXT,
                selectcolor=BG_MAIN,
                activebackground=BG_CARD,
                font=("Segoe UI", 10),
                anchor="w",
            ).pack(fill="x", padx=12, pady=2)

        comment_var = tk.StringVar()
        comment_row = tk.Frame(container, bg=BG_MAIN)
        comment_row.pack(fill="x", pady=(10, 0))
        tk.Label(comment_row, text="Комментарий", bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
        tk.Entry(
            comment_row,
            textvariable=comment_var,
            bg=BG_CARD,
            fg=FG_TEXT,
            relief="flat",
            bd=0,
            font=("Segoe UI", 10),
            highlightbackground=BORDER,
            highlightthickness=1,
            insertbackground=FG_TEXT,
        ).pack(side="left", fill="x", expand=True)

        actions = tk.Frame(container, bg=BG_MAIN)
        actions.pack(fill="x", pady=(14, 0))

        def confirm_release():
            dialog.destroy()
            ScanningActionsMixin.release_busy_kiz(self, code, reason_var.get(), comment_var.get())

        AppButton(
            actions,
            text="✅ БЛОК У МЕНЯ, ОСВОБОДИТЬ",
            bg=SUCCESS,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            command=confirm_release,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        AppButton(
            actions,
            text="СКАНИРОВАТЬ ДРУГОЙ",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            command=dialog.destroy,
        ).pack(side="right")

    def release_busy_kiz(self, code, reason, comment=""):
        """Call the backend release endpoint and let the operator scan the block."""
        try:
            result = release_backend_kiz(
                code,
                reason,
                comment=comment,
                workstation_id=socket.gethostname(),
            )
        except Exception as exc:
            self.show_error(f"Не удалось освободить КИЗ: {exc}")
            return

        if not (result or {}).get("released"):
            outcome = normalize_text((result or {}).get("outcome"))
            if outcome == "already_available":
                self.all_existing_codes.discard(code)
                self.show_error("КИЗ уже свободен, сканируйте блок ещё раз", popup=False)
            else:
                self.show_error(f"Backend не освободил КИЗ: {outcome or 'причина не указана'}")
            return

        self.all_existing_codes.discard(code)
        donor = normalize_text((result or {}).get("donor_request_number"))
        donor_note = f" (снят с заявки {donor})" if donor else ""
        self.status_var.set(f"🔓 КИЗ освобождён{donor_note}, отсканируйте блок ещё раз")
        logging.info("KIZ released by operator: reason=%s donor=%s", reason, donor or "none")
        ScanningActionsMixin.focus_scan_entry(self)

    def open_scan_codes_manager(self):
        """List every code of the current item so the picker can undo any of them."""
        if not self.current_order:
            self.show_error("Нет активной позиции")
            return

        if not self.scanned_codes:
            self.show_error("Нет кодов для отмены")
            return

        dialog = tk.Toplevel(self)
        dialog.title("Коды позиции")
        dialog.configure(bg=BG_MAIN)
        dialog.geometry("700x440")
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_MAIN, padx=16, pady=16)
        container.pack(fill="both", expand=True)

        product = normalize_text(self.current_order.get("Товары", "")) or "позиция без названия"
        tk.Label(
            container,
            text=f"Позиция: {product}",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        card = tk.Frame(container, bg=BG_CARD, bd=1, highlightbackground=BORDER)
        card.pack(fill="both", expand=True)

        code_list = tk.Listbox(
            card,
            bg=BG_CARD,
            fg=FG_TEXT,
            relief="flat",
            font=("Consolas", 10),
            selectmode="browse",
            activestyle="none",
        )
        code_list.pack(fill="both", expand=True, padx=12, pady=12)
        for position, code in enumerate(self.scanned_codes, start=1):
            state = "сохранён" if position <= self.saved_codes_count else "в очереди"
            code_list.insert(tk.END, f"{position:>3}. {code}  [{state}]")
        code_list.selection_set(tk.END)
        code_list.see(tk.END)

        tk.Label(
            container,
            text="Блок вернулся на склад? Выберите его код и отмените, иначе КИЗ останется занятым",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        actions = tk.Frame(container, bg=BG_MAIN)
        actions.pack(fill="x", pady=(10, 0))

        def undo_selected():
            selection = code_list.curselection()
            if not selection:
                self.show_error("Сначала выберите код в списке")
                return
            index = int(selection[0])
            dialog.destroy()
            ScanningActionsMixin.undo_scan_at_index(self, index)

        AppButton(
            actions,
            text="↩️ ОТМЕНИТЬ ВЫБРАННЫЙ",
            bg=DANGER,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            command=undo_selected,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        AppButton(
            actions,
            text="ЗАКРЫТЬ",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            command=dialog.destroy,
        ).pack(side="right")

    def undo_scan_at_index(self, index):
        """Undo any code of the current item, not only the last one.

        A picker who spots a wrong block several scans later used to have no way
        back: the code stayed on the item forever while the block returned to the
        shelf, and its KIZ stayed busy for good.
        """
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

        if not 0 <= index < len(self.scanned_codes):
            self.show_error("Код не найден в этой позиции")
            return

        removed_code = self.scanned_codes.pop(index)
        remaining_codes = self.scanned_codes.copy()
        # Codes are appended in scan order, so the first saved_codes_count of them
        # are the ones the backend already stored.
        was_saved = index < self.saved_codes_count

        if not write_scan_backup("undo_scan", self.current_order, code=removed_code, codes=remaining_codes):
            self.scanned_codes.insert(index, removed_code)
            self.show_error("Не удалось сохранить локальный backup отмены. Код не отменён")
            return

        if was_saved and order_uses_backend_scan_path(self.current_order):
            try:
                undo_backend_scan(self.current_order, removed_code)
            except Exception as exc:
                self.scanned_codes.insert(index, removed_code)
                self.show_error(f"Не удалось отменить код в VDS: {exc}")
                return
            self.saved_codes_count -= 1
        elif was_saved:
            self.scanned_codes.insert(index, removed_code)
            self.show_error("Позиция не связана с backend. Отмена заблокирована")
            return

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
        undo_label = "Отменён последний код" if index == len(remaining_codes) else "Отменён код из списка"
        self.status_var.set(f"↩️ {undo_label} ({scanned_count}/{plan_blocks})")

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
            ScanningActionsMixin.reject_scan(self, "Требуется обновить приложение перед сканированием", popup=False)
            return

        if self.operation_in_progress:
            self.show_busy_error()
            ScanningActionsMixin.clear_scan_entry_value(self)
            ScanningActionsMixin.focus_scan_entry(self)
            return

        if not self.current_order:
            ScanningActionsMixin.reject_scan(self, "Сначала выберите заказ")
            return

        is_valid, error_msg, code = validate_kiz_code(self.scan_entry.get())
        if not code:
            return

        if not is_valid:
            ScanningActionsMixin.reject_scan(self, error_msg)
            return

        block_reason = blocked_kiz_reason(code)
        if block_reason:
            logging.warning("Blocked KIZ scan attempt rejected on desktop")
            ScanningActionsMixin.reject_scan(self, f"🚫 {block_reason}")
            return

        plan_blocks = get_plan_blocks(self.current_order)
        if plan_blocks <= 0:
            ScanningActionsMixin.reject_scan(self, "В заказе не указано корректное 'Кол-во блок'")
            return

        scanned_before = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        if scanned_before >= plan_blocks:
            ScanningActionsMixin.reject_scan(self, f"План выполнен! Нельзя сканировать больше {plan_blocks} блоков")
            return

        scan_metadata = scan_metadata_for_code(code)
        block_quantity = scan_metadata["block_quantity"]
        product_name = self.current_order.get("Товары", "")
        if scan_product_mismatch(code, product_name):
            ScanningActionsMixin.reject_scan(
                self,
                format_scan_product_mismatch_message(
                    code,
                    product_name,
                    scan_product_key=scan_metadata.get("product_key") or "",
                )
            )
            return
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            if aggregate_product_mismatch(code, product_name):
                ScanningActionsMixin.reject_scan(self, "Код короба не соответствует товару текущей позиции")
                return
            remaining_blocks = max(0, plan_blocks - scanned_before)
            if block_quantity > remaining_blocks:
                ScanningActionsMixin.reject_scan(self, f"Короб +{block_quantity} блоков превышает остаток позиции: осталось {remaining_blocks}")
                return

        if code in self.scanned_codes:
            ScanningActionsMixin.reject_scan(self, "Код уже отсканирован в этой позиции")
            return

        duplicate_in_completed_orders = any(
            code in completed.get("Коды", [])
            for completed in self.completed_orders
        )
        if code in self.all_existing_codes or duplicate_in_completed_orders:
            existing_order = (
                find_code_owner_in_orders(code, self.today_orders)
                if code in self.all_existing_codes
                else {}
            )
            reuse_status = backend_duplicate_scan_reuse_status(self.current_order, code)
            if reuse_status.get("available"):
                self.all_existing_codes.discard(code)
                logging.info("Backend released KIZ for re-scan after return/undo/reset; ignoring stale desktop duplicate state")
            else:
                if code in self.all_existing_codes:
                    message = format_duplicate_scan_message(code, existing_order, reuse_status)
                    self.log_duplicate_code_async(code)
                else:
                    message = "Код уже использован в другом задании сегодня"
                ScanningActionsMixin.reject_scan(self, message)
                # A busy KIZ used to be a dead end: offer the way out right here,
                # so a block that never left the warehouse can still ship.
                ScanningActionsMixin.prompt_kiz_release(self, code, message)
                return

        if not order_uses_backend_scan_path(self.current_order):
            ScanningActionsMixin.reject_scan(self, "Позиция не связана с backend. Сканирование заблокировано")
            return

        if not write_scan_backup("scan", self.current_order, code=code, codes=self.scanned_codes + [code]):
            ScanningActionsMixin.reject_scan(self, "Не удалось сохранить локальный backup. Код не принят")
            return

        self.scanned_codes.append(code)
        self.all_existing_codes.add(code)
        queue_backend_scan(self.current_order, code)
        self.current_order["_existing_scan_entries"] = scan_entries_for_order_codes(self.current_order, self.scanned_codes)
        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)

        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            self.last_code_label.config(text=f"Последний код: короб +{block_quantity}: {code[:40]}...", fg=SUCCESS)
            message = f"Отсканирован короб +{block_quantity} ({scanned_count}/{plan_blocks})"
            self.status_var.set(f"✅ {message}")
        else:
            self.last_code_label.config(text=f"Последний код: {code[:40]}...", fg=SUCCESS)
            message = f"Отсканирован код ({scanned_count}/{plan_blocks})"
            self.status_var.set(f"✅ {message}")
        ScanningActionsMixin.accept_scan(self, message)
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
        ScanningActionsMixin.clear_scan_entry_value(self)

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
            if not order_uses_backend_scan_path(order):
                raise RuntimeError("Позиция не связана с backend. Сохранение КИЗов заблокировано")
            for saved_code in unsaved_backend_scan_codes(order, scanned_codes):
                if not queue_backend_scan(order, saved_code):
                    raise RuntimeError("Не удалось поставить КИЗ в durable backend-очередь")
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
                self.set_scan_entry_enabled(False, "SKU-защита недоступна: все позиции сохранены.")
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
