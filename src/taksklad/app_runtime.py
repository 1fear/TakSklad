import logging
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

from .config import (
    APP_NAME,
    BG_MAIN,
    ERROR_BG,
    ERROR_FG,
    FG_MUTED,
    FG_TEXT,
    LOG_FILE,
    WARNING,
)
from .sheets import release_telegram_poll_lock
from .telegram_service import telegram_single_listener_lock_enabled
from .utils import normalize_text


STATUS_NOTICE_TIMEOUT_MS = 5000


def format_exception_message(title, exc):
    return (
        f"{title}\n\n"
        f"Причина: {exc}\n\n"
        f"Подробности записаны в лог:\n{LOG_FILE}"
    )


def show_startup_error_message(title, message):
    try:
        root = tk.Tk()
        root.title(title)
        root.configure(bg=ERROR_BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        tk.Label(
            root,
            text=f"❌ {message}",
            bg=ERROR_BG,
            fg=ERROR_FG,
            font=("Segoe UI", 11, "bold"),
            padx=24,
            pady=18,
            wraplength=560,
            justify="left",
        ).pack(fill="both", expand=True)
        root.update_idletasks()
        x = max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)
        y = max(root.winfo_screenheight() - root.winfo_height() - 80, 0)
        root.geometry(f"+{x}+{y}")
        root.after(STATUS_NOTICE_TIMEOUT_MS, root.destroy)
        root.mainloop()
    except Exception:
        pass


def show_exception_message(title, exc):
    logging.exception(title)
    show_startup_error_message("Ошибка", format_exception_message(title, exc))


def global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error(
        "Неперехваченная ошибка",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    show_startup_error_message(
        "Критическая ошибка",
        format_exception_message("Неперехваченная ошибка", exc_value),
    )


class AppRuntimeMixin:

    def run_background(self, title, work, on_success=None, on_error=None, on_finally=None):
        def worker():
            try:
                result = work()
            except Exception as exc:
                logging.exception(title)

                def fail(exc=exc):
                    try:
                        if on_error:
                            on_error(exc)
                        else:
                            self.show_critical_error(title, exc)
                    finally:
                        if on_finally:
                            on_finally()

                try:
                    self.after(0, fail)
                except tk.TclError:
                    pass
                return

            def done():
                try:
                    if on_success:
                        on_success(result)
                finally:
                    if on_finally:
                        on_finally()

            try:
                self.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()


    def set_busy(self, message):
        self.operation_in_progress = True
        self.operation_started_at = time.monotonic()
        self.operation_message = normalize_text(message)
        logging.info("Операция начата: %s", self.operation_message)
        self.status_var.set(message)
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)


    def clear_busy(self):
        if self.operation_in_progress:
            elapsed = None
            if self.operation_started_at is not None:
                elapsed = time.monotonic() - self.operation_started_at
            if elapsed is None:
                logging.info("Операция завершена: %s", self.operation_message)
            else:
                logging.info("Операция завершена: %s (%.1f сек.)", self.operation_message, elapsed)
        self.operation_in_progress = False
        self.operation_started_at = None
        self.operation_message = ""


    def set_refresh_in_progress(self, message):
        self.refresh_in_progress = True
        self.refresh_started_at = time.monotonic()
        self.refresh_message = normalize_text(message)
        self.refresh_notice_token += 1
        notice_token = self.refresh_notice_token
        logging.info("Фоновое обновление начато: %s", self.refresh_message)
        self.status_var.set(message)
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        try:
            self.after(15000, lambda token=notice_token: self.show_refresh_long_running_notice(token))
        except tk.TclError:
            pass


    def clear_refresh_in_progress(self):
        if self.refresh_in_progress:
            elapsed = None
            if self.refresh_started_at is not None:
                elapsed = time.monotonic() - self.refresh_started_at
            if elapsed is None:
                logging.info("Фоновое обновление завершено: %s", self.refresh_message)
            else:
                logging.info("Фоновое обновление завершено: %s (%.1f сек.)", self.refresh_message, elapsed)
        self.refresh_in_progress = False
        self.refresh_started_at = None
        self.refresh_message = ""
        self.refresh_notice_token += 1


    def safe_config(self, widget, **kwargs):
        try:
            if widget is not None and widget.winfo_exists():
                widget.config(**kwargs)
        except tk.TclError:
            logging.debug("UI: виджет уже недоступен при изменении состояния", exc_info=True)


    def show_busy_error(self):
        message = "Дождитесь завершения текущей операции"
        if self.operation_message:
            message += f": {self.operation_message}"
            if self.operation_started_at is not None:
                elapsed = int(time.monotonic() - self.operation_started_at)
                message += f" ({elapsed} сек.)"
        self.show_error(message)


    def show_refresh_busy_error(self):
        message = "Обновление списка уже идёт в фоне"
        if self.refresh_started_at is not None:
            elapsed = int(time.monotonic() - self.refresh_started_at)
            message += f" ({elapsed} сек.)"
        message += ". Можно продолжать работу с уже загруженным списком."
        self.show_error(message)


    def show_refresh_long_running_notice(self, notice_token=None):
        if not self.refresh_in_progress or notice_token != self.refresh_notice_token:
            return
        elapsed = 0
        if self.refresh_started_at is not None:
            elapsed = int(time.monotonic() - self.refresh_started_at)
        self.status_var.set(
            f"⏳ Обновление списка всё ещё идёт ({elapsed} сек.). "
            "Можно продолжать работу с уже загруженным списком."
        )
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        try:
            self.after(15000, lambda token=notice_token: self.show_refresh_long_running_notice(token))
        except tk.TclError:
            pass


    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")


    def show_status_notice(self, message, *, bg, fg, prefix, log_level=None):
        text = normalize_text(message)
        if log_level is not None:
            logging.log(log_level, "Уведомление пользователю: %s", text)
        self.status_var.set(f"{prefix} {text}" if prefix else text)
        self.safe_config(self.status_label, bg=bg, fg=fg)
        if self.error_timer:
            try:
                self.after_cancel(self.error_timer)
            except tk.TclError:
                pass
        self.error_timer = self.after(STATUS_NOTICE_TIMEOUT_MS, self.clear_error)


    def show_error_toast(self, message):
        if not hasattr(self, "error_toast"):
            return
        text = normalize_text(message)
        self.error_toast.set_text(text)
        if not self.toast_visible:
            self.error_toast.pack(fill="x", pady=(0, 12), before=self.status_label)
            self.toast_visible = True


    def hide_error_toast(self):
        if not hasattr(self, "error_toast") or not self.toast_visible:
            return
        try:
            self.error_toast.pack_forget()
        except tk.TclError:
            pass
        self.toast_visible = False


    def show_error(self, message, popup=True):
        logging.warning("Ошибка для пользователя: %s", message)
        self.show_status_notice(message, bg=ERROR_BG, fg=ERROR_FG, prefix="❌", log_level=None)
        show_toast = getattr(self, "show_error_toast", None)
        if callable(show_toast) and hasattr(self, "error_toast"):
            show_toast(message)
        if hasattr(self, "last_code_label"):
            self.safe_config(
                self.last_code_label,
                text=f"Ошибка: {normalize_text(message)}",
                fg=ERROR_FG,
            )


    def show_warning(self, message):
        self.show_status_notice(message, bg=WARNING, fg=FG_TEXT, prefix="⚠", log_level=logging.WARNING)


    def show_info(self, message):
        self.show_status_notice(message, bg=BG_MAIN, fg=FG_MUTED, prefix="✅", log_level=logging.INFO)


    def show_critical_error(self, title, exc_or_message):
        if isinstance(exc_or_message, BaseException):
            message = str(exc_or_message)
            logging.error(
                title,
                exc_info=(type(exc_or_message), exc_or_message, exc_or_message.__traceback__)
            )
            detail = format_exception_message(title, exc_or_message)
        else:
            message = str(exc_or_message)
            logging.error("%s: %s", title, message)
            detail = f"{title}\n\nПричина: {message}\n\nПодробности записаны в лог:\n{LOG_FILE}"

        self.show_error(f"{title}: {message}", popup=False)
        self.send_telegram_alert_async(f"{APP_NAME}: ошибка приложения\n\n" + detail[:3800])


    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        logging.error(
            "Ошибка в интерфейсе",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        try:
            self.show_error(f"Ошибка в интерфейсе: {exc_value}", popup=False)
            detail = format_exception_message("Ошибка в интерфейсе", exc_value)
            self.send_telegram_alert_async(f"{APP_NAME}: ошибка интерфейса\n\n" + detail[:3800])
        except Exception:
            pass


    def clear_error(self):
        self.hide_error_toast()
        if self.update_required:
            self.status_var.set("⛔ Требуется обновление приложения")
            self.safe_config(self.status_label, bg=ERROR_BG, fg=ERROR_FG)
            self.error_timer = None
            return
        if self.refresh_in_progress:
            self.status_var.set(self.refresh_message or "⏳ Обновляю список заказов в фоне...")
            self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
            self.error_timer = None
            return
        self.status_var.set("✅ Готов к работе")
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        self.error_timer = None


    def on_close(self):
        if self.current_order and len(self.scanned_codes) > self.saved_codes_count:
            if not messagebox.askyesno(
                "Закрыть программу?",
                "Есть несохранённые сканы по текущей позиции.\n\nЗакрыть программу без завершения позиции?"
            ):
                return
        if telegram_single_listener_lock_enabled():
            try:
                release_telegram_poll_lock(self.telegram_lock_owner_id)
            except Exception:
                logging.info("Telegram: lock не освобождён при закрытии", exc_info=True)
        self.destroy()
