import tkinter as tk
from tkinter import messagebox

from .catalog import delete_product_rule, load_product_catalog, product_catalog_key, upsert_product_rule
from .config import (
    ACCENT,
    BG_CARD,
    BG_MAIN,
    BORDER,
    DANGER,
    DEFAULT_PIECES_PER_BLOCK,
    FG_MUTED,
    FG_TEXT,
    INFO,
    SUCCESS,
)
from .ui_widgets import AppButton
from .utils import normalize_lookup_text, normalize_text, parse_int_value


class CatalogActionsMixin:
    def show_product_catalog(self):
        if not self.ensure_update_allowed():
            return

        catalog = load_product_catalog()
        dialog = tk.Toplevel(self)
        dialog.title("Справочник товаров")
        dialog.configure(bg=BG_MAIN)
        dialog.geometry("720x480")
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_MAIN, padx=16, pady=16)
        container.pack(fill="both", expand=True)

        left = tk.Frame(container, bg=BG_CARD, bd=1, highlightbackground=BORDER)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))

        tk.Label(left, text="Товары", bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 8))
        product_list = tk.Listbox(left, bg=BG_CARD, fg=FG_TEXT, relief="flat", font=("Segoe UI", 10))
        product_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        right = tk.Frame(container, bg=BG_CARD, bd=1, highlightbackground=BORDER)
        right.pack(side="right", fill="both", expand=True)

        tk.Label(right, text="Карточка товара", bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 8))

        name_var = tk.StringVar()
        pieces_var = tk.StringVar(value=str(DEFAULT_PIECES_PER_BLOCK))
        requires_var = tk.BooleanVar(value=True)
        selected_key = {"value": None}

        def field(label, variable):
            row = tk.Frame(right, bg=BG_CARD)
            row.pack(fill="x", padx=12, pady=5)
            tk.Label(row, text=label, bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 10), width=18, anchor="w").pack(side="left")
            entry = tk.Entry(
                row,
                textvariable=variable,
                bg=BG_MAIN,
                fg=FG_TEXT,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10),
                highlightbackground=BORDER,
                highlightcolor=ACCENT,
                highlightthickness=1,
                insertbackground=FG_TEXT,
            )
            entry.pack(side="left", fill="x", expand=True)
            return entry

        field("Название", name_var)
        field("ШТ в блоке", pieces_var)
        tk.Checkbutton(
            right,
            text="Нужен КИЗ",
            variable=requires_var,
            bg=BG_CARD,
            fg=FG_TEXT,
            activebackground=BG_CARD,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=12, pady=(4, 8))

        def catalog_items():
            return sorted(catalog.items(), key=lambda item: normalize_lookup_text(item[1].get("name") or item[0]))

        def refresh_list():
            product_list.delete(0, tk.END)
            for _, item in catalog_items():
                product_list.insert(tk.END, f"{item.get('name', '')} | {parse_int_value(item.get('pieces_per_block')) or DEFAULT_PIECES_PER_BLOCK} шт.")

        def on_select(_event=None):
            selection = product_list.curselection()
            if not selection:
                return
            key, item = catalog_items()[selection[0]]
            selected_key["value"] = key
            name_var.set(item.get("name", ""))
            pieces_var.set(str(parse_int_value(item.get("pieces_per_block")) or DEFAULT_PIECES_PER_BLOCK))
            requires_var.set(bool(item.get("requires_kiz", True)))

        def save_current():
            name = normalize_text(name_var.get())
            pieces = parse_int_value(pieces_var.get())
            if not name:
                self.show_error("Укажите название товара")
                return
            if pieces <= 0:
                self.show_error("ШТ в блоке должно быть больше нуля")
                return

            old_key = selected_key.get("value")
            new_key = product_catalog_key(name)
            rule = {
                "name": name,
                "pieces_per_block": pieces,
                "requires_kiz": bool(requires_var.get()),
            }
            updated_catalog = upsert_product_rule(old_key, new_key, rule)
            catalog.clear()
            catalog.update(updated_catalog)
            selected_key["value"] = new_key
            self.product_catalog = catalog
            refresh_list()
            self.status_var.set("✅ Справочник товаров сохранён")

        def new_product():
            selected_key["value"] = None
            name_var.set("")
            pieces_var.set(str(DEFAULT_PIECES_PER_BLOCK))
            requires_var.set(True)

        def delete_product():
            key = selected_key.get("value")
            if not key:
                return
            if messagebox.askyesno("Удалить товар?", "Удалить выбранный товар из справочника?"):
                updated_catalog = delete_product_rule(key)
                catalog.clear()
                catalog.update(updated_catalog)
                new_product()
                refresh_list()

        product_list.bind("<<ListboxSelect>>", on_select)
        refresh_list()

        actions = tk.Frame(right, bg=BG_CARD)
        actions.pack(fill="x", padx=12, pady=(12, 0))
        AppButton(actions, text="СОХРАНИТЬ", bg=SUCCESS, fg="white", font=("Segoe UI", 9, "bold"), relief="flat", command=save_current).pack(side="left", fill="x", expand=True, padx=(0, 6))
        AppButton(actions, text="НОВЫЙ", bg=INFO, fg="white", font=("Segoe UI", 9, "bold"), relief="flat", command=new_product).pack(side="left", fill="x", expand=True, padx=(0, 6))
        AppButton(actions, text="УДАЛИТЬ", bg=DANGER, fg="white", font=("Segoe UI", 9, "bold"), relief="flat", command=delete_product).pack(side="left", fill="x", expand=True)

        close_frame = tk.Frame(right, bg=BG_CARD)
        close_frame.pack(fill="x", padx=12, pady=(16, 12))
        AppButton(close_frame, text="ЗАКРЫТЬ", bg=FG_MUTED, fg="white", font=("Segoe UI", 9, "bold"), relief="flat", command=dialog.destroy).pack(side="right")
