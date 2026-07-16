from datetime import datetime, timezone
import urllib.parse

import httpx
from .redaction import redact_secrets
from .telegram_clients import TelegramProcessorDelegate
from .telegram_common import (
    display_date,
    format_money,
    iso_date_from_display,
    normalize_text,
    parse_int,
    telegram_inline_keyboard,
)


TELEGRAM_BUTTON_KIZ_BY_FILES = "Выгрузка КИЗов"
TELEGRAM_KIZ_FILE_PREFIX = "КИЗ файл "
TELEGRAM_KIZ_DATE_PREFIX = "КИЗ дата "
TELEGRAM_KIZ_RANGE_CALLBACK_PREFIX = "kiz_range:"
TELEGRAM_DATE_MENU_RECENT_LIMIT = 7


def kiz_progress_completed(item):
    item = item or {}
    if "completed" in item:
        return bool(item.get("completed"))
    return parse_int(item.get("scanned_blocks")) >= parse_int(item.get("planned_blocks"))


def recent_logistics_dates_for_menu(dates, limit=TELEGRAM_DATE_MENU_RECENT_LIMIT):
    dates = list(dates or [])
    if len(dates) <= limit:
        return dates
    return sorted(
        dates,
        key=lambda value: iso_date_from_display(value),
    )[-limit:]


def kiz_dates_for_menu(dates):
    return sorted(
        list(dates or []),
        key=lambda item: iso_date_from_display((item or {}).get("date") or ""),
    )


def kiz_date_range_for_menu(dates):
    iso_dates = [
        iso_date_from_display((item or {}).get("date") or "")
        for item in dates or []
        if iso_date_from_display((item or {}).get("date") or "")
    ]
    if len(iso_dates) < 2:
        return "", ""
    return iso_dates[0], iso_dates[-1]


def kiz_source_file_uploaded_at(item):
    uploaded_at = normalize_text((item or {}).get("uploaded_at")).replace("Z", "+00:00")
    if uploaded_at:
        try:
            parsed = datetime.fromisoformat(uploaded_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass

    dates = [
        iso_date_from_display(value)
        for value in ((item or {}).get("dates") or [])
        if iso_date_from_display(value)
    ]
    if not dates:
        return 0
    return datetime.fromisoformat(max(dates)).replace(tzinfo=timezone.utc).timestamp()


def kiz_source_file_is_telegram_upload(item):
    return normalize_text((item or {}).get("import_source")).casefold() == "telegram"


def recent_kiz_source_files_for_menu(files, limit=TELEGRAM_DATE_MENU_RECENT_LIMIT):
    files = list(files or [])
    return sorted(
        files,
        key=lambda item: (
            kiz_source_file_is_telegram_upload(item),
            kiz_source_file_uploaded_at(item),
            normalize_text((item or {}).get("source_file")),
            normalize_text((item or {}).get("source_key")),
        ),
        reverse=True,
    )[:limit]


def backend_http_error_detail(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return redact_secrets(normalize_text(exc))[:300]
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and normalize_text(payload.get("detail")):
        return redact_secrets(normalize_text(payload.get("detail")))[:300]
    text = redact_secrets(normalize_text(getattr(response, "text", "")))
    return text[:300] or f"HTTP {getattr(response, 'status_code', '')}".strip()


def backend_failure_message(action, exc):
    action = normalize_text(action) or "Действие не выполнено"
    if isinstance(exc, httpx.HTTPStatusError):
        detail = backend_http_error_detail(exc)
        return f"{action}: {detail or 'backend вернул ошибку'}"
    if isinstance(exc, httpx.HTTPError):
        return f"{action}: backend временно недоступен ({exc.__class__.__name__})"
    detail = redact_secrets(normalize_text(exc))[:300]
    return f"{action}: {detail or exc.__class__.__name__}"


def summarize_active_orders_by_date(orders):
    summary = {}
    for order in orders or []:
        date_key = normalize_text(order.get("order_date")) or "Без даты"
        bucket = summary.setdefault(date_key, {
            "orders": 0,
            "items": 0,
            "planned_blocks": 0,
            "scanned_blocks": 0,
            "remaining_blocks": 0,
            "missing_skladbot": 0,
            "total_price": 0,
        })
        bucket["orders"] += 1
        if not normalize_text(order.get("skladbot_request_number")) and not normalize_text(order.get("skladbot_request_id")):
            bucket["missing_skladbot"] += 1
        for item in order.get("items") or []:
            planned = parse_int(item.get("quantity_blocks"))
            scanned = parse_int(item.get("scanned_blocks"))
            bucket["items"] += 1
            bucket["planned_blocks"] += planned
            bucket["scanned_blocks"] += scanned
            bucket["remaining_blocks"] += max(0, planned - scanned)
            bucket["total_price"] += parse_int(item.get("line_total"))
    return summary


class TelegramReportProcessor(TelegramProcessorDelegate):
    def __init__(self, *, ports=None, owner=None, **port_dependencies):
        TelegramProcessorDelegate.__init__(self, ports=ports, owner=owner, **port_dependencies)

    def logistics_date_keyboard(self, dates):
        rows = []
        for date_value in dates:
            iso_date = iso_date_from_display(date_value)
            if not iso_date:
                continue
            rows.append([{
                "text": display_date(date_value),
                "callback_data": f"logistics:{iso_date}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_files_keyboard(self, files):
        rows = []
        for index, item in enumerate(files, start=1):
            if not kiz_progress_completed(item):
                continue
            source_file = normalize_text(item.get("source_file")) or f"Файл {index}"
            text = source_file if len(source_file) <= 40 else source_file[:37] + "..."
            rows.append([{
                "text": f"{index}. {text}",
                "callback_data": f"kiz_file:{index}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_dates_keyboard(self, dates):
        rows = []
        start_date, end_date = kiz_date_range_for_menu(dates)
        if start_date and end_date:
            rows.append([{
                "text": f"Выгрузить все даты ({display_date(start_date)}-{display_date(end_date)})",
                "callback_data": f"{TELEGRAM_KIZ_RANGE_CALLBACK_PREFIX}{start_date}:{end_date}",
            }])
        for index, item in enumerate(dates, start=1):
            date_value = normalize_text(item.get("date"))
            iso_date = iso_date_from_display(date_value)
            if not iso_date:
                continue
            rows.append([{
                "text": f"{index}. {display_date(iso_date)}",
                "callback_data": f"kiz_date:{iso_date}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_export_mode_keyboard(self):
        return telegram_inline_keyboard([
            [{"text": "По датам отгрузки", "callback_data": "kiz_mode:dates"}],
            [{"text": "По загруженным Excel-файлам", "callback_data": "kiz_mode:files"}],
        ])

    def send_logistics_report(self, chat_id, shipment_date):
        iso_date = iso_date_from_display(shipment_date)
        if not iso_date:
            self.safe_send_message(chat_id, "Не понял дату. Используйте формат 29.05.2026.")
            return False
        report_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes("/api/v1/logistics/report", params={"shipment_date": iso_date})
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить отчёт логистики за {report_date}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить отчёт логистики за {report_date}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename = f"TakSklad_логистика_{report_date}_MANUAL.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"MANUAL /logistics · Отчёт логистики за {report_date}",
        )
        return True

    def show_logistics_dates(self, chat_id):
        try:
            dates = self.backend_get("/api/v1/logistics/dates")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить даты логистики", exc))
            return
        dates = dates if isinstance(dates, list) else []
        dates = recent_logistics_dates_for_menu(dates)
        if not dates:
            self.safe_send_message(chat_id, "Нет доступных дат отгрузки для отчёта логистики.")
            return
        self.safe_send_message(chat_id, "Выберите дату отгрузки для отчёта логистики:", reply_markup=self.logistics_date_keyboard(dates))

    def show_kiz_export_menu(self, chat_id):
        self.safe_send_message(
            chat_id,
            "Как выгрузить КИЗы?",
            reply_markup=self.kiz_export_mode_keyboard(),
        )

    def show_kiz_dates(self, chat_id):
        try:
            dates = self.backend_get("/api/v1/reports/kiz/dates")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить даты КИЗов", exc))
            return
        dates = dates if isinstance(dates, list) else []
        dates = kiz_dates_for_menu(dates)
        if not dates:
            self.safe_send_message(chat_id, "Нет дат отгрузки с отсканированными КИЗами.")
            return
        if len(dates) == 1:
            self.send_kiz_date_report(chat_id, dates[0].get("date") or "")
            return

        state = self.get_chat_state(chat_id)
        state["kiz_dates"] = [
            {
                "index": index,
                "date": item.get("date") or "",
            }
            for index, item in enumerate(dates, start=1)
        ]
        self.save_chat_state(chat_id, state)
        lines = ["Выберите дату отгрузки для выгрузки КИЗов:"]
        start_date, end_date = kiz_date_range_for_menu(dates)
        if start_date and end_date:
            lines.append(f"0. Все даты - {display_date(start_date)}-{display_date(end_date)}")
        for index, item in enumerate(dates, start=1):
            completed = kiz_progress_completed(item)
            status = "готово" if completed else f"частично, осталось {item.get('remaining_blocks', 0)}"
            lines.append(
                f"{index}. {display_date(item.get('date'))} - "
                f"{item.get('scanned_blocks', 0)}/{item.get('planned_blocks', 0)} блоков, {status}"
            )
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=self.kiz_dates_keyboard(dates))

    def show_kiz_source_files(self, chat_id):
        try:
            files = self.backend_get("/api/v1/reports/kiz/source-files")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить список Excel-файлов для КИЗов", exc))
            return
        files = files if isinstance(files, list) else []
        files = recent_kiz_source_files_for_menu(files)
        if not files:
            self.safe_send_message(chat_id, "Нет загруженных Excel-файлов для выгрузки КИЗов.")
            return

        state = self.get_chat_state(chat_id)
        state["kiz_files"] = [
            {
                "index": index,
                "source_file": item.get("source_file") or "",
                "source_key": item.get("source_key") or "",
                "completed": kiz_progress_completed(item),
            }
            for index, item in enumerate(files, start=1)
        ]
        self.save_chat_state(chat_id, state)

        ready_lines = []
        pending_lines = []
        for index, item in enumerate(files, start=1):
            dates = ", ".join(display_date(value) for value in item.get("dates") or [])
            completed = kiz_progress_completed(item)
            status = "готов к выгрузке" if completed else f"не готов, осталось {item.get('remaining_blocks', 0)}"
            date_suffix = f" | даты: {dates}" if dates else ""
            target = ready_lines if completed else pending_lines
            target.append(
                f"{index}. {item.get('source_file') or 'без файла'} - "
                f"{item.get('scanned_blocks', 0)}/{item.get('planned_blocks', 0)} блоков, {status}{date_suffix}"
            )
        lines = ["Загруженные Excel-файлы:"]
        if ready_lines:
            lines.extend(["", "Готово к выгрузке:", *ready_lines])
        if pending_lines:
            lines.extend(["", "Ещё не готово:", *pending_lines])

        keyboard = self.kiz_files_keyboard(files)
        self.safe_send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=keyboard if keyboard.get("inline_keyboard") else None,
        )

    def send_kiz_date_report(self, chat_id, shipment_date):
        iso_date = iso_date_from_display(shipment_date)
        if not iso_date:
            self.safe_send_message(chat_id, "Не понял дату. Используйте формат 05.06.2026.")
            return False
        report_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes("/api/v1/reports/kiz/date", params={"shipment_date": iso_date})
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за {report_date}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за {report_date}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{report_date}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы за дату отгрузки {report_date}",
        )
        return True

    def send_kiz_range_report(self, chat_id, date_from, date_to):
        iso_from = iso_date_from_display(date_from)
        iso_to = iso_date_from_display(date_to)
        if not iso_from or not iso_to:
            self.safe_send_message(chat_id, "Не понял период. Используйте формат: /kiz 04.06.2026 05.06.2026.")
            return False
        display_from = datetime.strptime(iso_from, "%Y-%m-%d").strftime("%d.%m.%Y")
        display_to = datetime.strptime(iso_to, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes(
                "/api/v1/reports/kiz/range",
                params={"date_from": iso_from, "date_to": iso_to},
            )
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за период {display_from}-{display_to}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за период {display_from}-{display_to}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{display_from}-{display_to}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы за период {display_from}-{display_to}",
        )
        return True

    def send_kiz_source_file_report(self, chat_id, source_file, source_key=""):
        source_file = normalize_text(source_file)
        source_key = normalize_text(source_key)
        if not source_file:
            self.safe_send_message(chat_id, "Не выбран исходный файл для выгрузки КИЗов.")
            return False
        params = {"source_file": source_file}
        if source_key:
            params["source_key"] = source_key
        try:
            content, headers = self.backend_get_bytes("/api/v1/reports/kiz/source-file", params=params)
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message(f"Не удалось выгрузить КИЗы по файлу {source_file}", exc))
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{source_file}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы по исходному файлу: {source_file}",
        )
        return True

    def send_imports_report(self, chat_id):
        try:
            payload = self.backend_get("/api/v1/imports")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить историю импортов", exc))
            return False
        imports = payload if isinstance(payload, list) else []
        if not imports:
            self.safe_send_message(chat_id, "История импортов пока пустая.")
            return True
        lines = ["Последние импорты TakSklad:"]
        for index, item in enumerate(imports[:10], start=1):
            raw_payload = item.get("raw_payload") or {}
            filename = normalize_text(raw_payload.get("filename")) or "без файла"
            lines.append(
                f"{index}. {filename}: {item.get('status')} "
                f"{item.get('rows_imported', 0)}/{item.get('rows_total', 0)}"
            )
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def send_status_report(self, chat_id):
        try:
            payload = self.backend_get("/api/v1/reports/day")
            active_orders = self.backend_get("/api/v1/orders/active")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить статус TakSklad", exc))
            return False
        totals = payload.get("totals") or {}
        report_date = display_date(payload.get("report_date")) or "сегодня"
        active_summary = summarize_active_orders_by_date(active_orders if isinstance(active_orders, list) else [])
        lines = [
            f"Статус TakSklad за {report_date}",
            "",
            f"Сегодня выполнено заказов: {totals.get('completed_orders', 0)}",
            f"КИЗов сегодня: {totals.get('scanned_today', 0)}",
            f"Всего КИЗов в отчёте: {totals.get('scan_codes', 0)}",
        ]
        if not active_summary:
            lines.extend(["", "Активных заказов для КИЗов нет."])
            self.safe_send_message(chat_id, "\n".join(lines))
            return True

        total_active = {
            "orders": 0,
            "items": 0,
            "planned_blocks": 0,
            "scanned_blocks": 0,
            "remaining_blocks": 0,
            "missing_skladbot": 0,
            "total_price": 0,
        }
        lines.extend(["", "Активные заказы для КИЗов:"])
        for date_key, values in sorted(active_summary.items()):
            for key in total_active:
                total_active[key] += values[key]
            lines.append(
                f"- {display_date(date_key) or date_key}: "
                f"{values['orders']} заказов, "
                f"{values['scanned_blocks']}/{values['planned_blocks']} блоков, "
                f"осталось {values['remaining_blocks']}, "
                f"без SkladBot {values['missing_skladbot']}, "
                f"{format_money(values['total_price'])} сум"
            )

        lines.extend([
            "",
            "Итого активно:",
            f"Заказов: {total_active['orders']}",
            f"Позиций: {total_active['items']}",
            f"Блоков: {total_active['scanned_blocks']} / {total_active['planned_blocks']}",
            f"Осталось блоков: {total_active['remaining_blocks']}",
            f"Без номера SkladBot: {total_active['missing_skladbot']}",
            f"Сумма активных заказов: {format_money(total_active['total_price'])} сум",
        ])
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def send_kiz_source_file_by_index(self, chat_id, text):
        index = parse_int(text.replace(TELEGRAM_KIZ_FILE_PREFIX, "", 1))
        state = self.get_chat_state(chat_id)
        files = state.get("kiz_files") or []
        selected = next((item for item in files if parse_int(item.get("index")) == index), None)
        if not selected:
            self.safe_send_message(chat_id, f"Не нашёл выбранный файл. Нажмите «{TELEGRAM_BUTTON_KIZ_BY_FILES}» ещё раз.")
            return False
        return self.send_kiz_source_file_report(
            chat_id,
            selected.get("source_file") or "",
            selected.get("source_key") or "",
        )

    def send_kiz_date_by_index(self, chat_id, text):
        index = parse_int(text.replace(TELEGRAM_KIZ_DATE_PREFIX, "", 1))
        state = self.get_chat_state(chat_id)
        dates = state.get("kiz_dates") or []
        selected = next((item for item in dates if parse_int(item.get("index")) == index), None)
        if not selected:
            self.safe_send_message(chat_id, f"Не нашёл выбранную дату. Нажмите «{TELEGRAM_BUTTON_KIZ_BY_FILES}» ещё раз.")
            return False
        return self.send_kiz_date_report(chat_id, selected.get("date") or "")
