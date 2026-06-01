import tkinter as tk
from tkinter import messagebox

from .config import (
    ACCENT,
    BG_CARD,
    BG_MAIN,
    BORDER,
    FG_MUTED,
    FG_TEXT,
    LABEL_DPI,
    LABEL_HEIGHT_MM,
    LABEL_WIDTH_MM,
    SUCCESS,
)
from .pending_store import load_pending_prints, remove_pending_print
from .printing import load_print_settings, print_summary, save_print_settings
from .printing import LABEL_SIZE_OPTIONS, label_size_to_text, list_available_printers, parse_label_size_text
from .ui_widgets import AppButton
from .utils import normalize_text


class PrintingActionsMixin:
    def confirm_print_settings(self):
        result = {"print": False}
        settings = load_print_settings()
        dialog = tk.Toplevel(self)
        dialog.title("Параметры печати")
        dialog.configure(bg=BG_MAIN)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_CARD, padx=24, pady=20)
        container.pack(fill="both", expand=True, padx=16, pady=16)

        tk.Label(
            container,
            text="Печать сводного листа",
            bg=BG_CARD,
            fg=FG_TEXT,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", pady=(0, 12))

        printer_var = tk.StringVar(value=settings.get("printer_name", "Термопринтер"))
        size_var = tk.StringVar(value=label_size_to_text(
            settings.get("label_width_mm", LABEL_WIDTH_MM),
            settings.get("label_height_mm", LABEL_HEIGHT_MM),
        ))
        save_var = tk.BooleanVar(value=True)
        available_printers = list_available_printers()

        printer_row = tk.Frame(container, bg=BG_CARD)
        printer_row.pack(fill="x", pady=3)
        tk.Label(printer_row, text="Принтер:", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 10), width=18, anchor="w").pack(side="left")
        if available_printers:
            if normalize_text(printer_var.get()) not in available_printers:
                printer_var.set(available_printers[0])
            tk.OptionMenu(printer_row, printer_var, *available_printers).pack(side="left", fill="x", expand=True)
        else:
            tk.Entry(
                printer_row,
                textvariable=printer_var,
                bg=BG_MAIN,
                fg=FG_TEXT,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10),
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
                highlightthickness=1,
                insertbackground=FG_TEXT,
            ).pack(side="left", fill="x", expand=True)

        size_row = tk.Frame(container, bg=BG_CARD)
        size_row.pack(fill="x", pady=3)
        tk.Label(size_row, text="Размер этикетки:", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 10), width=18, anchor="w").pack(side="left")
        tk.OptionMenu(size_row, size_var, *[label_size_to_text(width, height) for width, height in LABEL_SIZE_OPTIONS]).pack(side="left", fill="x", expand=True)

        rows = [
            ("Масштаб", settings.get("scale", "100%")),
            ("Разрешение макета", f"{LABEL_DPI} DPI"),
        ]

        for label, value in rows:
            row = tk.Frame(container, bg=BG_CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label + ":", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 10), width=18, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left")

        tk.Checkbutton(
            container,
            text="Запомнить параметры",
            variable=save_var,
            bg=BG_CARD,
            fg=FG_TEXT,
            activebackground=BG_CARD,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(8, 0))

        actions = tk.Frame(container, bg=BG_CARD)
        actions.pack(fill="x", pady=(18, 0))

        dialog_closed = {"value": False}

        def close_dialog():
            if dialog_closed["value"]:
                return
            dialog_closed["value"] = True
            try:
                dialog.unbind_all("<Return>")
                dialog.unbind_all("<KP_Enter>")
                dialog.unbind_all("<Escape>")
            except tk.TclError:
                pass
            dialog.destroy()

        def confirm():
            if dialog_closed["value"]:
                return
            result["print"] = True
            label_width, label_height = parse_label_size_text(size_var.get())
            if save_var.get():
                save_print_settings({
                    "printer_name": normalize_text(printer_var.get()) or "Термопринтер",
                    "label_width_mm": label_width,
                    "label_height_mm": label_height,
                    "dpi": LABEL_DPI,
                    "scale": "100%",
                })
            close_dialog()

        def cancel():
            close_dialog()

        print_button = AppButton(
            actions,
            text="ПЕЧАТАТЬ",
            bg=SUCCESS,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=18,
            pady=8,
            command=confirm,
            cursor="hand2",
        )
        print_button.pack(side="right", padx=(8, 0))

        AppButton(
            actions,
            text="ОТМЕНА",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            padx=18,
            pady=8,
            command=cancel,
            cursor="hand2",
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<KP_Enter>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.bind_all("<Return>", lambda _event: confirm())
        dialog.bind_all("<KP_Enter>", lambda _event: confirm())
        dialog.bind_all("<Escape>", lambda _event: cancel())
        self.update_idletasks()
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")
        print_button.focus_set()
        self.wait_window(dialog)
        return result["print"]

    def check_pending_prints(self):
        if self.operation_in_progress:
            self.after(1000, self.check_pending_prints)
            return

        pending = load_pending_prints()
        if not pending:
            return

        if not messagebox.askyesno(
            "Непечатанные сводки",
            f"Найдено непечатанных сводок: {len(pending)}.\n\nНапечатать сейчас?",
        ):
            return

        if not self.confirm_print_settings():
            return

        self.set_busy("⏳ Печатаю сводки из очереди...")

        def work():
            printed_count = 0
            for item in pending[:]:
                printed_files = print_summary(item.get("address", "Адрес не указан"), item.get("products", []))
                if not printed_files:
                    raise RuntimeError("Не удалось напечатать сводку из очереди")
                remove_pending_print(item.get("id"))
                printed_count += 1
            return printed_count

        def on_success(printed_count):
            self.status_var.set(f"✅ Напечатано сводок из очереди: {printed_count}")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)

        def on_error(exc):
            self.show_critical_error("Не удалось напечатать сводки из очереди", exc)

        def on_finally():
            self.clear_busy()

        self.run_background(
            "Не удалось напечатать сводки из очереди",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )
