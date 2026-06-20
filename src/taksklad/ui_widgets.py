import tkinter as tk

from .config import ACCENT, DISABLED_BG, DISABLED_FG


def darken_hex(color, factor=0.9):
    color = color.lstrip("#")
    if len(color) != 6:
        return "#" + color
    try:
        red = int(color[0:2], 16)
        green = int(color[2:4], 16)
        blue = int(color[4:6], 16)
    except ValueError:
        return "#" + color
    return "#{:02x}{:02x}{:02x}".format(
        max(0, int(red * factor)),
        max(0, int(green * factor)),
        max(0, int(blue * factor)),
    )


def fade_hex(color, amount=0.10):
    color = color.lstrip("#")
    if len(color) != 6:
        return "#" + color
    try:
        red = int(color[0:2], 16)
        green = int(color[2:4], 16)
        blue = int(color[4:6], 16)
    except ValueError:
        return "#" + color
    amount = max(0.0, min(1.0, amount))
    return "#{:02x}{:02x}{:02x}".format(
        min(255, int(red + (255 - red) * amount)),
        min(255, int(green + (255 - green) * amount)),
        min(255, int(blue + (255 - blue) * amount)),
    )


class AppButton(tk.Frame):
    def __init__(
        self,
        parent,
        text="",
        bg=ACCENT,
        fg="white",
        font=("Segoe UI", 10, "bold"),
        command=None,
        state="normal",
        padx=14,
        pady=8,
        cursor="hand2",
        disabled_bg=DISABLED_BG,
        disabled_fg=DISABLED_FG,
        radius=16,
        hover_bg=None,
        hover_fade=0.10,
        **kwargs
    ):
        frame_kwargs = {
            "bg": bg,
            "bd": 0,
            "highlightthickness": 0,
            "takefocus": 0,
        }
        for key in ("width", "height"):
            if key in kwargs:
                frame_kwargs[key] = kwargs[key]
        super().__init__(parent, **frame_kwargs)
        self._text = text
        self._normal_bg = bg
        self._normal_fg = fg
        self._hover_bg = hover_bg
        self._hover_fade = hover_fade
        self._active_bg = hover_bg or fade_hex(bg, hover_fade)
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._command = command
        self._state = state
        self._cursor = cursor
        self._font = font
        self._padx = padx
        self._pady = pady
        self._radius = radius

        self.canvas = tk.Canvas(
            self,
            bg=parent.cget("bg") if hasattr(parent, "cget") else bg,
            highlightthickness=0,
            bd=0,
            height=max(38, pady * 2 + 20),
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._refresh_style())

        for widget in (self, self.canvas):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

        self.config(state=state)

    def _on_click(self, _event=None):
        if self._state != "normal":
            return "break"
        if self._command:
            self._command()
        return "break"

    def _on_enter(self, _event=None):
        if self._state == "normal":
            self._paint(self._active_bg, self._normal_fg)

    def _on_leave(self, _event=None):
        self._refresh_style()

    def _paint(self, bg, fg):
        tk.Frame.configure(self, bg=bg)
        self.canvas.delete("all")
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self._rounded_rect(1, 1, width - 1, height - 1, self._radius, fill=bg, outline=bg)
        self.canvas.create_text(
            width // 2,
            height // 2,
            text=self._text,
            fill=fg,
            font=self._font,
            anchor="center",
        )

    def _rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
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
        return self.canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)

    def _refresh_active_bg(self):
        self._active_bg = self._hover_bg or fade_hex(self._normal_bg, self._hover_fade)

    def _refresh_style(self):
        if self._state == "normal":
            self._paint(self._normal_bg, self._normal_fg)
            tk.Frame.configure(self, cursor=self._cursor)
            self.canvas.configure(cursor=self._cursor)
        else:
            self._paint(self._disabled_bg, self._disabled_fg)
            tk.Frame.configure(self, cursor="")
            self.canvas.configure(cursor="")

    def configure(self, cnf=None, **kwargs):
        options = {}
        if cnf:
            options.update(cnf)
        options.update(kwargs)

        if "state" in options:
            self._state = options.pop("state")
        if "text" in options:
            self._text = options.pop("text")
        if "command" in options:
            self._command = options.pop("command")
        if "bg" in options:
            self._normal_bg = options.pop("bg")
            self._refresh_active_bg()
        if "background" in options:
            self._normal_bg = options.pop("background")
            self._refresh_active_bg()
        if "fg" in options:
            self._normal_fg = options.pop("fg")
        if "foreground" in options:
            self._normal_fg = options.pop("foreground")
        if "font" in options:
            self._font = options.pop("font")
        if "cursor" in options:
            self._cursor = options.pop("cursor")
        if "pady" in options:
            self._pady = options.pop("pady")
            self.canvas.configure(height=max(38, self._pady * 2 + 20))
        if "padx" in options:
            self._padx = options.pop("padx")
        if "radius" in options:
            self._radius = options.pop("radius")
        if "hover_bg" in options:
            self._hover_bg = options.pop("hover_bg")
            self._refresh_active_bg()
        if "hover_fade" in options:
            self._hover_fade = options.pop("hover_fade")
            self._refresh_active_bg()

        ignored = {
            "relief", "activebackground", "activeforeground",
            "selectbackground", "selectforeground", "disabledforeground",
        }
        for key in list(options.keys()):
            if key in ignored:
                options.pop(key)

        if options:
            tk.Frame.configure(self, **options)
        self._refresh_style()

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return tk.Frame.cget(self, key)


class RoundedNotice(tk.Frame):
    def __init__(
        self,
        parent,
        bg,
        fg,
        font=("Segoe UI", 10, "bold"),
        radius=22,
        padx=18,
        pady=12,
        **kwargs
    ):
        super().__init__(parent, bg=parent.cget("bg") if hasattr(parent, "cget") else bg, bd=0, highlightthickness=0)
        self._notice_bg = bg
        self._notice_fg = fg
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self.canvas = tk.Canvas(self, bg=self.cget("bg"), highlightthickness=0, bd=0, height=64, **kwargs)
        self.canvas.pack(fill="x", expand=True)
        self.label = tk.Label(
            self.canvas,
            text="",
            bg=bg,
            fg=fg,
            font=font,
            justify="left",
            anchor="w",
        )
        self._label_window = self.canvas.create_window(padx, pady, anchor="nw", window=self.label)
        self.canvas.bind("<Configure>", lambda _event: self._refresh())

    def set_text(self, text):
        self.label.config(text=text)
        self.after_idle(self._resize_to_text)

    def _resize_to_text(self):
        width = max(260, self.canvas.winfo_width() or self.winfo_width() or 900)
        wrap = max(220, width - self._padx * 2)
        self.label.config(wraplength=wrap)
        self.update_idletasks()
        height = max(58, self.label.winfo_reqheight() + self._pady * 2)
        self.canvas.config(height=height)
        self._refresh()

    def _refresh(self):
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        wrap = max(220, width - self._padx * 2)
        self.label.config(wraplength=wrap)
        self.canvas.coords(self._label_window, self._padx, self._pady)
        self.canvas.itemconfig(self._label_window, width=wrap)
        self.canvas.delete("notice_bg")
        self._rounded_rect(1, 1, width - 1, height - 1, self._radius, fill=self._notice_bg, outline=self._notice_bg)
        self.canvas.tag_lower("notice_bg")

    def _rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
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
        return self.canvas.create_polygon(points, smooth=True, tags=("notice_bg",), **kwargs)
