MAIN_SCREEN_REQUIRED_WIDGETS = (
    "order_list_subtitle_label",
    "search_entry",
    "order_card_list",
    "order_list_counter_label",
    "select_btn",
    "returns_btn",
    "current_info",
    "product_photo_canvas",
    "product_photo_gtin_label",
    "product_photo_caption_label",
    "current_client_label",
    "current_product_label",
    "party_summary_label",
    "position_label",
    "progress_label",
    "scan_entry",
    "scan_guard_label",
    "last_code_label",
    "undo_btn",
    "next_product_btn",
    "finish_btn",
    "completed_count_label",
    "total_blocks_label",
    "active_orders_label",
    "pending_saves_label",
    "sync_caption_label",
    "backend_status_label",
    "sync_queue_btn",
    "diagnostics_btn",
    "report_btn",
    "error_toast",
    "status_var",
    "status_label",
    "version_status_label",
)

MAIN_SCREEN_TEXT_EXPECTATIONS = {
    "order_list_subtitle_label": "активных заказов",
    "search_entry": "Поиск клиента",
    "order_list_counter_label": "Показаны",
    "select_btn": "ВЫБРАТЬ ЗАКАЗ",
    "returns_btn": "ВОЗВРАТЫ",
    "current_info": "Не выбрано",
    "product_photo_gtin_label": "GTIN",
    "product_photo_caption_label": "Фото товара",
    "party_summary_label": "Партия не выбрана",
    "progress_label": "0 / 0",
    "scan_guard_label": "SKU-защита недоступна",
    "undo_btn": "ОТМЕНИТЬ ПОСЛЕДНИЙ КОД",
    "next_product_btn": "СЛЕДУЮЩАЯ ПОЗИЦИЯ",
    "finish_btn": "ЗАВЕРШИТЬ ЗАКАЗ",
    "completed_count_label": "0",
    "total_blocks_label": "0",
    "active_orders_label": "0",
    "pending_saves_label": "OK",
    "sync_caption_label": "Синхронизация",
    "sync_queue_btn": "ОЧЕРЕДИ",
    "diagnostics_btn": "ДИАГНОСТИКА",
    "report_btn": "ЗАКРЫТЬ СМЕНУ",
    "version_status_label": "Версия:",
}

MAIN_SCREEN_DISABLED_BUTTONS = ("scan_entry", "undo_btn", "next_product_btn", "finish_btn")


def run_tk_app_smoke(app_factory):
    app = app_factory()
    try:
        app.update_idletasks()
        validate_main_screen_smoke_snapshot(collect_main_screen_smoke_snapshot(app))
        return 0
    finally:
        app.destroy()


def collect_main_screen_smoke_snapshot(app):
    missing = []
    texts = {}
    states = {}
    for name in MAIN_SCREEN_REQUIRED_WIDGETS:
        if not hasattr(app, name) or getattr(app, name) is None:
            missing.append(name)
            continue
        widget = getattr(app, name)
        texts[name] = _widget_text(widget)
        state = _widget_option(widget, "state")
        if state:
            states[name] = state

    product_photo_size = (None, None)
    if hasattr(app, "product_photo_canvas") and getattr(app, "product_photo_canvas") is not None:
        canvas = app.product_photo_canvas
        product_photo_size = (_int_option(canvas, "width"), _int_option(canvas, "height"))

    order_card_list = getattr(app, "order_card_list", None)
    order_card_list_scrollable = bool(
        order_card_list
        and getattr(order_card_list, "canvas", None) is not None
        and getattr(order_card_list, "scrollbar", None) is not None
    )

    status_var = getattr(app, "status_var", None)
    status_text = ""
    if status_var is not None and hasattr(status_var, "get"):
        try:
            status_text = str(status_var.get())
        except Exception:
            status_text = ""

    return {
        "missing": missing,
        "texts": texts,
        "states": states,
        "product_photo_size": product_photo_size,
        "order_card_list_scrollable": order_card_list_scrollable,
        "status_text": status_text,
    }


def validate_main_screen_smoke_snapshot(snapshot):
    problems = []
    missing = snapshot.get("missing") or []
    if missing:
        problems.append("missing widgets: " + ", ".join(missing))

    texts = snapshot.get("texts") or {}
    for name, expected in MAIN_SCREEN_TEXT_EXPECTATIONS.items():
        actual = texts.get(name, "")
        if expected not in actual:
            problems.append(f"{name} text must contain {expected!r}")

    for name in MAIN_SCREEN_DISABLED_BUTTONS:
        if (snapshot.get("states") or {}).get(name) != "disabled":
            problems.append(f"{name} must start disabled")

    if "Готов" not in (snapshot.get("status_text") or ""):
        problems.append("status_var must start with ready message")

    if snapshot.get("product_photo_size") != (170, 170):
        problems.append("product_photo_canvas must be 170x170")

    if not snapshot.get("order_card_list_scrollable"):
        problems.append("order_card_list must expose canvas and scrollbar")

    if problems:
        raise RuntimeError("Main screen GUI smoke failed: " + "; ".join(problems))


def _widget_text(widget):
    if hasattr(widget, "_placeholder_text"):
        return str(getattr(widget, "_placeholder_text") or "")
    value = _widget_option(widget, "text")
    return str(value or "")


def _widget_option(widget, key):
    if not hasattr(widget, "cget"):
        return ""
    try:
        return widget.cget(key)
    except Exception:
        return ""


def _int_option(widget, key):
    try:
        return int(_widget_option(widget, key))
    except (TypeError, ValueError):
        return None
