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
        self._active_bg = darken_hex(bg, 0.92)
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._command = command
        self._state = state
        self._cursor = cursor

        self.label = tk.Label(
            self,
            text=text,
            bg=bg,
            fg=fg,
            font=font,
            bd=0,
            padx=padx,
            pady=pady,
            anchor="center"
        )
        self.label.pack(fill="both", expand=True)

        for widget in (self, self.label):
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
        self.label.configure(bg=bg, fg=fg)

    def _refresh_style(self):
        if self._state == "normal":
            self._paint(self._normal_bg, self._normal_fg)
            tk.Frame.configure(self, cursor=self._cursor)
            self.label.configure(cursor=self._cursor)
        else:
            self._paint(self._disabled_bg, self._disabled_fg)
            tk.Frame.configure(self, cursor="")
            self.label.configure(cursor="")

    def configure(self, cnf=None, **kwargs):
        options = {}
        if cnf:
            options.update(cnf)
        options.update(kwargs)

        if "state" in options:
            self._state = options.pop("state")
        if "text" in options:
            self._text = options.pop("text")
            self.label.configure(text=self._text)
        if "command" in options:
            self._command = options.pop("command")
        if "bg" in options:
            self._normal_bg = options.pop("bg")
            self._active_bg = darken_hex(self._normal_bg, 0.92)
        if "background" in options:
            self._normal_bg = options.pop("background")
            self._active_bg = darken_hex(self._normal_bg, 0.92)
        if "fg" in options:
            self._normal_fg = options.pop("fg")
        if "foreground" in options:
            self._normal_fg = options.pop("foreground")
        if "font" in options:
            self.label.configure(font=options.pop("font"))
        if "cursor" in options:
            self._cursor = options.pop("cursor")

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
