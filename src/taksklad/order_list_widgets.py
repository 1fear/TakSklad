import tkinter as tk

from .config import ACCENT, BG_CARD, BG_MAIN, BORDER, FG_MUTED, FG_TEXT
from .order_list_models import ORDER_CARD_KIND
from .ui_widgets import fade_hex


LIST_SURFACE_BG = "#fffaf2"
SELECTED_CARD_BG = "#fff6df"
PLACEHOLDER_FG = "#a7a095"
SCROLLBAR_BG = "#e5dcc8"
SCROLLBAR_THUMB = "#c4ad7a"


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(0, min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2)))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class _RoundedCard(tk.Frame):
    def __init__(self, parent, *, surface_bg, fill, border, radius=14, padx=22, pady=16):
        super().__init__(parent, bg=surface_bg, bd=0, highlightthickness=0, cursor="hand2")
        self._surface_bg = surface_bg
        self._fill = fill
        self._border = border
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self.canvas = tk.Canvas(
            self,
            bg=surface_bg,
            bd=0,
            highlightthickness=0,
            height=112,
            cursor="hand2",
        )
        self.canvas.pack(fill="x", expand=True)
        self.body = tk.Frame(self.canvas, bg=fill, bd=0, highlightthickness=0, cursor="hand2")
        self._body_window = self.canvas.create_window(padx, pady, anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.body.bind("<Configure>", self._on_body_configure)

    def set_style(self, *, fill, border):
        self._fill = fill
        self._border = border
        self.body.configure(bg=fill)
        self._redraw()

    def _on_body_configure(self, event=None):
        requested_height = (event.height if event else self.body.winfo_reqheight()) + self._pady * 2
        self.canvas.configure(height=max(104, requested_height))
        self._redraw()

    def _on_canvas_configure(self, event):
        width = max(1, event.width - self._padx * 2)
        self.canvas.itemconfigure(self._body_window, width=width)
        self._redraw()

    def _redraw(self):
        self.canvas.delete("card_bg")
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        _rounded_rect(
            self.canvas,
            1,
            1,
            width - 2,
            height - 2,
            self._radius,
            fill=self._fill,
            outline=self._border,
            width=2 if self._border == ACCENT else 1,
            tags=("card_bg",),
        )
        self.canvas.tag_lower("card_bg")


class PlaceholderEntry(tk.Frame):
    def __init__(
        self,
        parent,
        textvariable,
        placeholder="",
        font=("Segoe UI", 12),
        bg=LIST_SURFACE_BG,
        fg=FG_TEXT,
        placeholder_fg=PLACEHOLDER_FG,
        border=BORDER,
        focus_border=ACCENT,
        padx=38,
        pady=10,
        **kwargs,
    ):
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=border, bd=0, **kwargs)
        self._normal_border = border
        self._focus_border = focus_border
        self._variable = textvariable
        self._placeholder_text = placeholder
        self._placeholder_visible = False
        self._padx = padx

        self.icon_label = tk.Label(self, text="⌕", bg=bg, fg=placeholder_fg, font=("Segoe UI", 14, "bold"))
        self.icon_label.pack(side="left", padx=(13, 2), pady=pady)

        self.entry = tk.Entry(
            self,
            textvariable=textvariable,
            bg=bg,
            fg=fg,
            font=font,
            relief="flat",
            bd=0,
            highlightthickness=0,
            insertbackground=fg,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 14), pady=pady)

        self.placeholder_label = tk.Label(
            self,
            text=placeholder,
            bg=bg,
            fg=placeholder_fg,
            font=font,
            anchor="w",
        )

        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.placeholder_label.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self.icon_label.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self._trace_id = textvariable.trace_add("write", lambda *_: self._sync_placeholder())
        self._sync_placeholder()

    def focus_set(self):
        self.entry.focus_set()

    def bind_entry(self, sequence, callback):
        self.entry.bind(sequence, callback)

    def get(self):
        return self._variable.get()

    def _on_focus_in(self, _event=None):
        self.configure(highlightbackground=self._focus_border)
        self._sync_placeholder(force_hide=True)

    def _on_focus_out(self, _event=None):
        self.configure(highlightbackground=self._normal_border)
        self._sync_placeholder()

    def _sync_placeholder(self, force_hide=False):
        should_show = bool(self._placeholder_text) and not self._variable.get() and not force_hide
        if should_show == self._placeholder_visible:
            return
        self._placeholder_visible = should_show
        if should_show:
            self.placeholder_label.place(x=self._padx, y=0, relheight=1, relwidth=1, width=-self._padx - 12)
            self.placeholder_label.lift()
        else:
            self.placeholder_label.place_forget()


class OrderCardList(tk.Frame):
    def __init__(
        self,
        parent,
        *,
        on_activate=None,
        bg=BG_CARD,
        surface_bg=LIST_SURFACE_BG,
        border=BORDER,
        accent=ACCENT,
        **kwargs,
    ):
        super().__init__(parent, bg=bg, **kwargs)
        self._surface_bg = surface_bg
        self._card_bg = BG_CARD
        self._selected_bg = SELECTED_CARD_BG
        self._border = border
        self._accent = accent
        self._on_activate = on_activate
        self._rows = ()
        self._selected_key = None
        self._selectable_keys = []
        self._card_widgets = {}

        self.canvas = tk.Canvas(
            self,
            bg=surface_bg,
            highlightthickness=1,
            highlightbackground=border,
            bd=0,
            takefocus=1,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.scrollbar = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            bg=SCROLLBAR_THUMB,
            troughcolor=SCROLLBAR_BG,
            activebackground=fade_hex(SCROLLBAR_THUMB, 0.12),
            bd=0,
            relief="flat",
            width=16,
        )
        self.scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.inner = tk.Frame(self.canvas, bg=surface_bg)
        self._inner_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Up>", lambda event: self._move_selection(-1))
        self.canvas.bind("<Down>", lambda event: self._move_selection(1))
        self.canvas.bind("<Return>", self._activate_selected)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def set_rows(self, rows, selected_key=None):
        for child in self.inner.winfo_children():
            child.destroy()
        self._rows = tuple(rows or ())
        self._selectable_keys = []
        self._card_widgets = {}

        if not self._rows:
            self._selected_key = None
            empty = tk.Label(
                self.inner,
                text="Заказы не найдены",
                bg=self._surface_bg,
                fg=FG_MUTED,
                font=("Segoe UI", 11, "bold"),
                anchor="center",
                pady=28,
            )
            empty.pack(fill="x", padx=18, pady=26)
            self._bind_wheel(empty)
            self._update_scrollregion()
            return

        for row in self._rows:
            if getattr(row, "kind", "") == ORDER_CARD_KIND:
                self._add_card(row)
            else:
                self._add_date_separator(row)

        if selected_key in self._selectable_keys:
            self._selected_key = selected_key
        elif self._selected_key not in self._selectable_keys:
            self._selected_key = None
        self._refresh_cards()
        self._update_scrollregion()

    def selected_key(self):
        return self._selected_key

    def clear_selection(self):
        self._selected_key = None
        self._refresh_cards()

    def select_first_card(self):
        if not self._selectable_keys:
            self.clear_selection()
            return False
        return self.select_key(self._selectable_keys[0], scroll=True)

    def select_key(self, key, *, scroll=False):
        if key not in self._selectable_keys:
            return False
        self._selected_key = key
        self._refresh_cards()
        self.canvas.focus_set()
        if scroll:
            self.after_idle(lambda: self._scroll_to_key(key))
        return True

    def _add_date_separator(self, row):
        label = tk.Label(
            self.inner,
            text=getattr(row, "title", ""),
            bg=self._surface_bg,
            fg=FG_MUTED,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        label.pack(fill="x", padx=20, pady=(16, 7))
        self._bind_wheel(label)

    def _add_card(self, row):
        self._selectable_keys.append(row.group_key)
        card = _RoundedCard(
            self.inner,
            surface_bg=self._surface_bg,
            fill=self._card_bg,
            border=self._border,
        )
        card.pack(fill="x", padx=18, pady=(0, 12))
        card.body.grid_columnconfigure(0, weight=1)

        title = tk.Label(
            card.body,
            text=row.client,
            bg=self._card_bg,
            fg=FG_TEXT,
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            justify="left",
        )
        title.grid(row=0, column=0, sticky="ew", pady=(0, 7))

        meta = tk.Label(
            card.body,
            text=row.meta_text,
            bg=self._card_bg,
            fg=FG_MUTED,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        )
        meta.grid(row=1, column=0, sticky="ew", pady=(0, 5))

        summary = tk.Label(
            card.body,
            text=row.summary_text,
            bg=self._card_bg,
            fg=self._accent,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            justify="left",
        )
        summary.grid(row=2, column=0, sticky="ew")

        widgets = (card, card.canvas, card.body, title, meta, summary)
        self._card_widgets[row.group_key] = {
            "frame": card,
            "labels": (title, meta, summary),
            "title": title,
            "meta": meta,
            "summary": summary,
        }
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, key=row.group_key: self.select_key(key))
            widget.bind("<Double-Button-1>", lambda _event, key=row.group_key: self._activate_key(key))
            self._bind_wheel(widget)
        card.canvas.bind(
            "<Configure>",
            lambda event, labels=(title, meta, summary): self._sync_wraplength(event, labels),
            add="+",
        )

    def _refresh_cards(self):
        for key, widgets in self._card_widgets.items():
            selected = key == self._selected_key
            bg = self._selected_bg if selected else self._card_bg
            border = self._accent if selected else self._border
            frame = widgets["frame"]
            frame.set_style(fill=bg, border=border)
            widgets["title"].configure(bg=bg, fg=FG_TEXT)
            widgets["meta"].configure(bg=bg, fg=FG_MUTED)
            widgets["summary"].configure(bg=bg, fg=self._accent)

    def _move_selection(self, step):
        if not self._selectable_keys:
            return "break"
        if self._selected_key not in self._selectable_keys:
            index = 0
        else:
            index = self._selectable_keys.index(self._selected_key) + step
            index = max(0, min(index, len(self._selectable_keys) - 1))
        self.select_key(self._selectable_keys[index], scroll=True)
        return "break"

    def _activate_selected(self, _event=None):
        if self._selected_key is not None and self._on_activate:
            self._on_activate()
        return "break"

    def _activate_key(self, key):
        if self.select_key(key) and self._on_activate:
            self._on_activate()
        return "break"

    def _scroll_to_key(self, key):
        widgets = self._card_widgets.get(key)
        if not widgets:
            return
        self._update_scrollregion()
        frame = widgets["frame"]
        canvas_height = max(1, self.canvas.winfo_height())
        content_height = max(1, self.inner.winfo_height())
        if content_height <= canvas_height:
            self.canvas.yview_moveto(0)
            return
        target_y = max(0, frame.winfo_y() - 12)
        self.canvas.yview_moveto(min(1, target_y / content_height))

    def _sync_wraplength(self, event, labels):
        wraplength = max(160, event.width - 60)
        for label in labels:
            label.configure(wraplength=wraplength)

    def _on_inner_configure(self, _event=None):
        self._update_scrollregion()

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._inner_window, width=max(1, event.width))
        self._update_scrollregion()

    def _update_scrollregion(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._on_mousewheel)
        widget.bind("<Button-4>", self._on_mousewheel)
        widget.bind("<Button-5>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        if event.num == 4:
            direction = -1
        elif event.num == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(direction, "units")
        return "break"
