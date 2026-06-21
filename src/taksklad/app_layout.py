from datetime import datetime
import tkinter as tk

from .config import (
    ACCENT,
    BG_CARD,
    BG_MAIN,
    BORDER,
    DANGER,
    FG_MUTED,
    FG_TEXT,
    INFO,
    SUCCESS,
    WARNING,
)
from .startup_check import format_app_version_label
from .order_list_widgets import OrderCardList, PlaceholderEntry
from .ui_widgets import AppButton, RoundedNotice


PRODUCT_PHOTO_SIZE = 170
PRODUCT_PHOTO_BG = "#fffaf0"
PRODUCT_PHOTO_SHELL_BG = "#f3ead8"
UI_FONT = "Segoe UI"
TITLE_FONT = (UI_FONT, 22, "bold")
DATE_FONT = (UI_FONT, 11)
CARD_TITLE_FONT = (UI_FONT, 11, "bold")
LIST_TITLE_FONT = (UI_FONT, 16, "bold")
BODY_FONT = (UI_FONT, 10)
BODY_FONT_BOLD = (UI_FONT, 10, "bold")
SMALL_FONT = (UI_FONT, 9)
SMALL_FONT_BOLD = (UI_FONT, 9, "bold")
ENTRY_FONT = (UI_FONT, 13)
PRIMARY_LABEL_FONT = (UI_FONT, 15, "bold")
PRODUCT_LABEL_FONT = (UI_FONT, 15, "bold")
PROGRESS_FONT = (UI_FONT, 16, "bold")
KPI_FONT = (UI_FONT, 18, "bold")
KPI_LABEL_FONT = (UI_FONT, 9)
PRIMARY_BUTTON_FONT = (UI_FONT, 11, "bold")
ACTION_BUTTON_FONT = (UI_FONT, 10, "bold")


class LayoutMixin:
    def _build_ui(self):
        main = tk.Frame(self, bg=BG_MAIN)
        main.pack(fill="both", expand=True, padx=25, pady=20)

        title = tk.Label(main, text="📦 УЧЁТ СКАНИРОВАНИЯ БЛОКОВ",
                        bg=BG_MAIN, fg=FG_TEXT, font=TITLE_FONT)
        title.pack(pady=(0, 5))

        date_label = tk.Label(main, text=f"Дата: {datetime.now().strftime('%d.%m.%Y')}",
                             bg=BG_MAIN, fg=FG_MUTED, font=DATE_FONT)
        date_label.pack(pady=(0, 20))

        content = tk.Frame(main, bg=BG_MAIN)
        content.pack(fill="both", expand=True)

        left_panel = tk.Frame(content, bg=BG_MAIN)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 15))

        list_card = tk.Frame(left_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        list_card.pack(fill="both", expand=True)

        list_header = tk.Frame(list_card, bg=BG_CARD)
        list_header.pack(fill="x", padx=28, pady=(24, 6))

        tk.Label(list_header, text="Заказы для КИЗов",
                bg=BG_CARD, fg=FG_TEXT, font=LIST_TITLE_FONT).pack(side="left")

        self.refresh_btn = AppButton(list_header, text="↻ ОБНОВИТЬ",
                                     bg=INFO, fg="white", font=SMALL_FONT_BOLD,
                                     command=self.refresh_from_sheet, relief="flat", cursor="hand2",
                                     radius=16, pady=7)
        self.refresh_btn.pack(side="right")

        self.order_list_subtitle_label = tk.Label(
            list_card,
            text="0 активных заказов · список листается вниз",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=(UI_FONT, 10, "bold"),
            anchor="w",
        )
        self.order_list_subtitle_label.pack(fill="x", padx=28, pady=(0, 22))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_legal_list())
        self.search_entry = PlaceholderEntry(
            list_card,
            textvariable=self.search_var,
            placeholder="Поиск клиента, адреса или заявки",
            font=(UI_FONT, 12),
        )
        self.search_entry.pack(fill="x", padx=28, pady=(0, 22))

        self.import_btn = None
        self.catalog_btn = None
        self.control_btn = None

        list_container = tk.Frame(list_card, bg=BG_CARD)
        list_container.pack(fill="both", expand=True, padx=28, pady=(0, 0))

        self.order_card_list = OrderCardList(list_container, on_activate=self.select_legal_entity)
        self.order_card_list.pack(fill="both", expand=True)

        self.order_list_counter_label = tk.Label(
            list_card,
            text="Показаны 0 из 0",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=(UI_FONT, 9, "bold"),
            anchor="w",
        )
        self.order_list_counter_label.pack(fill="x", padx=32, pady=(8, 22))

        self.refresh_legal_list()

        self.select_btn = AppButton(left_panel, text="✅ ВЫБРАТЬ ЗАКАЗ",
                                   bg=ACCENT, fg="white", font=PRIMARY_BUTTON_FONT,
                                   command=self.select_legal_entity, relief="flat", pady=12,
                                   cursor="hand2", radius=22)
        self.select_btn.pack(pady=(15, 0), fill="x")

        self.returns_btn = AppButton(left_panel, text="↩ ВОЗВРАТЫ",
                                     bg=WARNING, fg=FG_TEXT, font=PRIMARY_BUTTON_FONT,
                                     command=self.show_returns_window, relief="flat", pady=12,
                                     cursor="hand2")
        self.returns_btn.pack(pady=(10, 0), fill="x")

        right_panel = tk.Frame(content, bg=BG_MAIN)
        right_panel.pack(side="right", fill="both", expand=True, padx=(15, 0))

        info_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        info_card.pack(fill="x", pady=(0, 15))

        tk.Label(info_card, text="📋 ТЕКУЩАЯ ПОЗИЦИЯ",
                bg=BG_CARD, fg=ACCENT, font=CARD_TITLE_FONT).pack(anchor="w", padx=20, pady=(15, 10))

        current_body = tk.Frame(info_card, bg=BG_CARD)
        current_body.pack(fill="x", padx=20, pady=(0, 12))

        current_text = tk.Frame(current_body, bg=BG_CARD)
        current_text.pack(side="left", fill="both", expand=True, padx=(0, 16))

        self.current_info = tk.Label(
            current_text,
            text="Не выбрано",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=BODY_FONT,
            wraplength=460,
            justify="left",
            anchor="nw",
        )
        self.current_info.pack(anchor="w", fill="x")

        product_photo_shell = tk.Frame(
            current_body,
            bg=PRODUCT_PHOTO_SHELL_BG,
            bd=1,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=7,
            pady=7,
        )
        product_photo_shell.pack(side="right", anchor="n")

        self.product_photo_canvas = tk.Canvas(
            product_photo_shell,
            width=PRODUCT_PHOTO_SIZE,
            height=PRODUCT_PHOTO_SIZE,
            bg=PRODUCT_PHOTO_BG,
            highlightthickness=1,
            highlightbackground=BORDER,
            bd=0,
        )
        self.product_photo_canvas.pack()

        self.product_photo_gtin_label = tk.Label(
            product_photo_shell,
            text="GTIN",
            bg=FG_TEXT,
            fg="#fff7df",
            font=SMALL_FONT_BOLD,
            padx=8,
            pady=6,
        )
        self.product_photo_gtin_label.pack(fill="x", pady=(8, 0))

        self.product_photo_caption_label = tk.Label(
            product_photo_shell,
            text="Фото товара",
            bg=PRODUCT_PHOTO_SHELL_BG,
            fg=FG_MUTED,
            font=SMALL_FONT,
        )
        self.product_photo_caption_label.pack(fill="x", pady=(6, 0))

        self.current_client_label = tk.Label(
            info_card,
            text="",
            bg=BG_CARD,
            fg=FG_TEXT,
            font=PRIMARY_LABEL_FONT,
            wraplength=620,
            justify="left",
            anchor="w",
        )
        self.current_client_label.pack(anchor="w", fill="x", padx=20, pady=(0, 6))

        self.current_product_label = tk.Label(
            info_card,
            text="",
            bg=BG_CARD,
            fg=ACCENT,
            font=PRODUCT_LABEL_FONT,
            wraplength=620,
            justify="left",
            anchor="w",
        )
        self.current_product_label.pack(anchor="w", fill="x", padx=20, pady=(0, 10))

        self.party_summary_label = tk.Label(
            info_card,
            text="Партия не выбрана",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=BODY_FONT,
            wraplength=620,
            justify="left",
        )
        self.party_summary_label.pack(anchor="w", padx=20, pady=(0, 10))

        progress_card = tk.Frame(
            info_card,
            bg=BG_MAIN,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        progress_card.pack(anchor="w", padx=20, pady=(0, 15), ipadx=16, ipady=8)

        self.position_label = tk.Label(progress_card, text="", bg=BG_MAIN, fg=FG_MUTED, font=SMALL_FONT_BOLD)
        self.position_label.pack(side="left", padx=(0, 18))

        self.progress_label = tk.Label(progress_card, text="0 / 0", bg=BG_MAIN, fg=SUCCESS, font=PROGRESS_FONT)
        self.progress_label.pack(side="left")

        scan_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        scan_card.pack(fill="x", pady=(0, 15))

        tk.Label(scan_card, text="🔍 СКАНИРОВАНИЕ КОДА",
                bg=BG_CARD, fg=ACCENT, font=CARD_TITLE_FONT).pack(anchor="w", padx=20, pady=(15, 10))

        self.scan_entry = tk.Entry(scan_card, bg=BG_MAIN, fg=FG_TEXT, font=ENTRY_FONT,
                                   relief="flat", bd=0, highlightbackground=BORDER,
                                   highlightcolor=ACCENT, highlightthickness=1,
                                   insertbackground=FG_TEXT)
        self.scan_entry.pack(fill="x", padx=20, pady=(0, 10))
        self.scan_entry.bind("<Return>", self.on_scan)

        self.last_code_label = tk.Label(scan_card, text="", bg=BG_CARD, fg=SUCCESS, font=BODY_FONT)
        self.last_code_label.pack(anchor="w", padx=20, pady=(5, 5))

        actions_frame = tk.Frame(right_panel, bg=BG_MAIN)
        actions_frame.pack(fill="x", pady=(0, 15))

        self.undo_btn = AppButton(actions_frame, text="↩️ ОТМЕНИТЬ ПОСЛЕДНИЙ КОД",
                                 bg=DANGER, fg="white", font=ACTION_BUTTON_FONT,
                                 command=self.undo_last_scan, relief="flat", state="disabled",
                                 cursor="hand2")
        self.undo_btn.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=5)

        self.next_product_btn = AppButton(actions_frame, text="➡️ СЛЕДУЮЩАЯ ПОЗИЦИЯ",
                                         bg=WARNING, fg=FG_TEXT, font=ACTION_BUTTON_FONT,
                                         command=self.next_product, relief="flat", state="disabled",
                                         cursor="hand2")
        self.next_product_btn.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=5)

        self.finish_btn = AppButton(actions_frame, text="🏁 ЗАВЕРШИТЬ ЗАКАЗ",
                                   bg=SUCCESS, fg="white", font=ACTION_BUTTON_FONT,
                                   command=self.finish_legal_entity, relief="flat", state="disabled",
                                   cursor="hand2")
        self.finish_btn.pack(side="right", fill="x", expand=True, padx=(10, 0), pady=5)

        stats_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        stats_card.pack(fill="x")

        tk.Label(stats_card, text="📊 СТАТИСТИКА",
                bg=BG_CARD, fg=ACCENT, font=CARD_TITLE_FONT).pack(anchor="w", padx=20, pady=(15, 10))

        stats_frame = tk.Frame(stats_card, bg=BG_CARD)
        stats_frame.pack(fill="x", padx=20, pady=(0, 14))

        def make_stat_tile(parent, value, caption, value_fg=FG_TEXT, padx=(0, 12)):
            tile = tk.Frame(
                parent,
                bg=BG_MAIN,
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=BORDER,
            )
            tile.pack(side="left", fill="x", expand=True, padx=padx)
            value_label = tk.Label(tile, text=value, bg=BG_MAIN, fg=value_fg, font=KPI_FONT, anchor="center")
            value_label.pack(fill="x", pady=(8, 0))
            caption_label = tk.Label(tile, text=caption, bg=BG_MAIN, fg=FG_MUTED, font=KPI_LABEL_FONT, anchor="center")
            caption_label.pack(fill="x", pady=(0, 8))
            return value_label, caption_label

        self.completed_count_label, self.completed_count_caption = make_stat_tile(stats_frame, "0", "Выполнено")
        self.total_blocks_label, self.total_blocks_caption = make_stat_tile(stats_frame, "0", "Блоков")
        self.active_orders_label, self.active_orders_caption = make_stat_tile(stats_frame, "0", "Активных заказов")
        self.pending_saves_label, self.sync_caption_label = make_stat_tile(
            stats_frame,
            "OK",
            "Синхронизация",
            value_fg=SUCCESS,
            padx=(0, 0),
        )

        stats_frame_3 = tk.Frame(stats_card, bg=BG_CARD)
        stats_frame_3.pack(fill="x", padx=20, pady=(0, 15))

        self.backend_status_label = tk.Label(
            stats_frame_3,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=BODY_FONT_BOLD,
        )
        self.backend_status_label.pack(side="left")

        self.report_btn = AppButton(right_panel, text="📊 ЗАКРЫТЬ СМЕНУ",
                                   bg=INFO, fg="white", font=ACTION_BUTTON_FONT,
                                   command=self.end_day, relief="flat", pady=10,
                                   cursor="hand2")
        self.report_btn.pack(fill="x", pady=(10, 0))

        status_frame = tk.Frame(main, bg=BG_MAIN)
        status_frame.pack(fill="x", pady=(20, 0))

        self.error_toast = RoundedNotice(
            status_frame,
            bg=DANGER,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            radius=8,
            padx=18,
            pady=14,
        )

        self.status_var = tk.StringVar(value="✅ Готов к работе")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 10),
                                     padx=14, pady=8, wraplength=900, justify="center")
        self.status_label.pack(fill="x")

        version_frame = tk.Frame(main, bg=BG_MAIN)
        version_frame.pack(fill="x", pady=(10, 0))
        tk.Label(
            version_frame,
            text=format_app_version_label(),
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
