import logging
import os
import threading
import time
from datetime import datetime

import tkinter as tk

from .catalog import load_product_catalog
from .config import (
    APP_NAME,
    BG_MAIN,
    DAILY_REPORT_CHECK_INTERVAL_MS,
    FG_MUTED,
    SHEET_NAME,
    SPREADSHEET_ID,
    TELEGRAM_LOCK_REFRESH_SECONDS,
    TELEGRAM_LOCK_RETRY_SECONDS,
    TELEGRAM_LOCK_TTL_SECONDS,
    TELEGRAM_DESKTOP_POLLING_ENABLED,
)
from .duplicate_codes import find_code_details_in_pending_saves, format_duplicate_code_details
from .excel_import import append_import_records, find_successful_import_by_file_hash, prepare_excel_import
from .reports import (
    build_document_summaries_from_gsheet,
    create_day_report_excel,
    create_document_report_excel,
    report_date_display,
)
from .sheets import (
    acquire_telegram_poll_lock,
    ensure_import_sheet_layout,
    find_code_details_in_sheet,
    get_google_client,
    read_shared_telegram_state,
    write_shared_telegram_state,
)
from .telegram_service import (
    TELEGRAM_CALLBACK_DOCUMENT_PREFIX,
    TELEGRAM_CALLBACK_DOCUMENTS,
    TELEGRAM_CALLBACK_TODAY_LOG,
    TELEGRAM_CALLBACK_TODAY_SCANS,
    answer_telegram_callback_query,
    create_today_log_file,
    download_telegram_document_to_temp,
    due_daily_report_dates,
    fetch_telegram_updates,
    load_telegram_settings,
    load_telegram_state,
    mark_daily_report_status,
    safe_telegram_document_path,
    update_telegram_state,
    send_daily_report_result_to_telegram,
    send_or_queue_telegram_document,
    send_telegram_document_to_chat,
    send_telegram_message,
    send_telegram_message_to_chat,
    sync_pending_telegram,
    telegram_chat_is_authorized,
    telegram_document_file_name,
    telegram_document_is_supported_excel,
    telegram_documents_keyboard,
    telegram_is_configured,
    telegram_reports_keyboard,
    telegram_single_listener_lock_enabled,
)
from .utils import file_sha256, normalize_text, parse_int_value


def desktop_telegram_polling_enabled(settings=None):
    return bool(TELEGRAM_DESKTOP_POLLING_ENABLED and telegram_is_configured(settings))


class TelegramActionsMixin:
    def send_telegram_documents_async(self, documents):
        documents = [
            (path, caption)
            for path, caption in documents
            if safe_telegram_document_path(path)
        ]
        if not documents:
            return

        def worker():
            for path, caption in documents:
                send_or_queue_telegram_document(path, caption)

        threading.Thread(target=worker, daemon=True).start()

    def sync_pending_telegram_async(self):
        def worker():
            result = sync_pending_telegram()
            if result.get("sent"):
                logging.info("Telegram: отправлено из очереди: %s", result["sent"])

        threading.Thread(target=worker, daemon=True).start()

    def check_daily_reports_async(self):
        if self.daily_report_check_running:
            return

        self.daily_report_check_running = True
        sheet = self.sheet

        def worker():
            results = []
            for report_date in due_daily_report_dates():
                try:
                    result = create_day_report_excel(sheet, report_date=report_date)
                    if result.get("empty"):
                        mark_daily_report_status(
                            report_date,
                            "empty",
                            message="Нет отсканированных КИЗов для отчёта",
                        )
                        results.append({
                            "date": report_date_display(report_date),
                            "status": "empty",
                            "message": "Нет данных",
                        })
                        continue

                    ok, message, status = send_daily_report_result_to_telegram(
                        result,
                        reason="Автоматическая отправка дневного отчёта",
                    )
                    results.append({
                        "date": result.get("report_date_display"),
                        "status": status,
                        "message": message,
                        "ok": ok,
                    })
                except Exception as exc:
                    logging.exception("Не удалось автоматически отправить дневной отчёт")
                    mark_daily_report_status(report_date, "failed", message=str(exc))
                    results.append({
                        "date": report_date_display(report_date),
                        "status": "failed",
                        "message": str(exc),
                    })
            return results

        def finish(results):
            self.daily_report_check_running = False
            for result in results:
                logging.info(
                    "Дневной отчёт %s: %s (%s)",
                    result.get("date"),
                    result.get("status"),
                    result.get("message"),
                )
            try:
                self.after(DAILY_REPORT_CHECK_INTERVAL_MS, self.check_daily_reports_async)
            except tk.TclError:
                pass

        def fail(exc):
            logging.error("Ошибка фоновой проверки дневного отчёта: %s", exc)
            self.daily_report_check_running = False
            try:
                self.after(DAILY_REPORT_CHECK_INTERVAL_MS, self.check_daily_reports_async)
            except tk.TclError:
                pass

        self.run_background(
            "Не удалось проверить дневные отчёты",
            worker,
            on_success=finish,
            on_error=fail,
        )

    def send_telegram_alert_async(self, message, with_keyboard=True):
        if not telegram_is_configured():
            return

        def worker():
            ok, result = send_telegram_message(
                message,
                reply_markup=telegram_reports_keyboard() if with_keyboard else None,
            )
            if not ok:
                logging.warning("Telegram: сообщение не отправлено: %s", result)

        threading.Thread(target=worker, daemon=True).start()

    def log_duplicate_code_async(self, code):
        current_order = dict(self.current_order or {})

        def worker():
            details = []
            try:
                sheet = self.sheet
                if not sheet:
                    client = get_google_client()
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
                details = find_code_details_in_sheet(sheet, code)
            except Exception:
                logging.exception("Не удалось определить строку дублирующегося КИЗа")
            details.extend(find_code_details_in_pending_saves(code))

            detail_text = format_duplicate_code_details(code, details, current_order=current_order)
            logging.warning("%s", detail_text)
            if telegram_is_configured():
                ok, result = send_telegram_message(
                    f"{APP_NAME}: найден дублирующийся КИЗ\n\n" + detail_text,
                    reply_markup=telegram_reports_keyboard(),
                )
                if not ok:
                    logging.warning("Telegram: дубль КИЗ не отправлен: %s", result)

        threading.Thread(target=worker, daemon=True).start()

    def get_sheet_for_telegram(self):
        if self.sheet:
            return self.sheet
        client = get_google_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        ensure_import_sheet_layout(sheet)
        self.sheet = sheet
        return sheet

    def send_today_scan_report_to_chat(self, chat_id, token):
        sheet = self.sheet
        result = create_day_report_excel(sheet, report_date=datetime.now().date())
        if result.get("empty") and not sheet:
            sheet = self.get_sheet_for_telegram()
            result = create_day_report_excel(sheet, report_date=datetime.now().date())
        if result.get("empty"):
            send_telegram_message_to_chat(
                chat_id,
                "За сегодня пока нет отсканированных КИЗов для отчёта.",
                token,
                reply_markup=telegram_reports_keyboard(),
            )
            return

        caption = (
            f"{APP_NAME}: сканы за {result.get('report_date_display')} на текущий момент\n"
            f"Всего КИЗов: {result['total_report_rows']}\n"
            f"Терминал: {result['terminal_count']}; "
            f"Перечисление: {result['transfer_count']}; "
            f"Не распознано: {result['unknown_count']}"
        )
        send_telegram_document_to_chat(result["filename"], chat_id, caption, token)

    def send_today_log_to_chat(self, chat_id, token):
        log_path = create_today_log_file()
        send_telegram_document_to_chat(
            log_path,
            chat_id,
            f"{APP_NAME}: сегодняшний лог по времени и ошибкам",
            token,
        )

    def send_document_list_to_chat(self, chat_id, token):
        sheet = self.get_sheet_for_telegram()
        documents = build_document_summaries_from_gsheet(sheet, limit=12)
        if not documents:
            send_telegram_message_to_chat(
                chat_id,
                "Импортированные документы в Google Sheets не найдены.",
                token,
                reply_markup=telegram_reports_keyboard(),
            )
            return

        lines = [f"{APP_NAME}: документы по импорту"]
        for idx, document in enumerate(documents, start=1):
            lines.append(
                f"{idx}. {document['source_file']} - "
                f"{document['scanned_blocks']}/{document['plan_blocks']} КИЗ, "
                f"позиций {document['completed_positions']}/{document['positions']}"
            )
        send_telegram_message_to_chat(
            chat_id,
            "\n".join(lines),
            token,
            reply_markup=telegram_documents_keyboard(documents),
        )

    def send_document_report_to_chat(self, chat_id, token, document_key):
        sheet = self.get_sheet_for_telegram()
        result = create_document_report_excel(sheet, document_key)
        if result.get("empty"):
            send_telegram_message_to_chat(
                chat_id,
                "Документ не найден. Обновите список документов в боте.",
                token,
                reply_markup=telegram_reports_keyboard(),
            )
            return

        caption = (
            f"{APP_NAME}: документ {result['source_file']}\n"
            f"КИЗ: {result['scanned_blocks']}/{result['plan_blocks']}\n"
            f"Позиций: {result['completed_positions']}/{result['positions']}\n"
            f"Осталось КИЗ: {result['remaining_blocks']}"
        )
        if result.get("pending_blocks"):
            caption += f"\nВ локальной очереди: {result['pending_blocks']}"
        send_telegram_document_to_chat(result["filename"], chat_id, caption, token)

    def send_telegram_menu_to_chat(self, chat_id, token):
        send_telegram_message_to_chat(
            chat_id,
            f"{APP_NAME}: выберите файл, который нужно получить, или отправьте Excel-файл для импорта.",
            token,
            reply_markup=telegram_reports_keyboard(),
        )

    def start_telegram_import_ui(self, file_name):
        self.status_var.set(f"⏳ Импортирую Excel из Telegram: {file_name}")
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        self.safe_config(self.import_btn, state="disabled")
        self.safe_config(self.refresh_btn, state="disabled")

    def finish_telegram_import_ui(self, status_message, loaded=None):
        try:
            if loaded is not None:
                self.product_catalog = load_product_catalog()
                self.apply_loaded_data(loaded, show_empty_warning=False)
                self.reset_current_selection()
                self.refresh_legal_list()
            self.status_var.set(status_message)
            self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        except Exception:
            logging.exception("Telegram: не удалось обновить интерфейс после импорта")
            self.status_var.set("Excel импортирован из Telegram, обновите список вручную")
        finally:
            self.clear_busy()
            if not self.update_required:
                self.safe_config(self.import_btn, state="normal")
                self.safe_config(self.refresh_btn, state="normal")

    def handle_telegram_document_message(self, document, chat_id, token):
        chat_id = normalize_text(chat_id)
        file_name = telegram_document_file_name(document)

        def safe_send(text, reply_markup=None):
            try:
                send_telegram_message_to_chat(chat_id, text, token, reply_markup=reply_markup)
            except Exception:
                logging.exception("Telegram: не удалось отправить ответ по импорту Excel")

        if not telegram_document_is_supported_excel(document):
            safe_send(
                "Файл не импортирован.\n\n"
                "Отправьте Excel-файл в формате .xlsx или .xlsm.",
                reply_markup=telegram_reports_keyboard(),
            )
            return

        if self.update_required:
            safe_send(
                f"Файл не импортирован: сначала нужно обновить {APP_NAME} на компьютере склада.",
                reply_markup=telegram_reports_keyboard(),
            )
            return

        if self.operation_in_progress:
            safe_send(
                f"Файл не импортирован: {APP_NAME} сейчас занят другой операцией. "
                "Отправьте Excel-файл повторно после завершения операции.",
                reply_markup=telegram_reports_keyboard(),
            )
            return

        self.operation_in_progress = True
        self.operation_started_at = time.monotonic()
        self.operation_message = f"Импорт Excel из Telegram: {file_name}"
        temp_path = None
        finish_scheduled = False

        def schedule_finish(status_message, loaded=None):
            nonlocal finish_scheduled
            if finish_scheduled:
                return
            finish_scheduled = True
            try:
                self.after(0, lambda: self.finish_telegram_import_ui(status_message, loaded=loaded))
            except tk.TclError:
                self.operation_in_progress = False
                self.operation_started_at = None
                self.operation_message = ""

        try:
            try:
                self.after(0, lambda: self.start_telegram_import_ui(file_name))
            except tk.TclError:
                pass

            safe_send(f"Получил Excel-файл: {file_name}\nНачинаю импорт в Google Sheets.")

            temp_path, source_file_name = download_telegram_document_to_temp(token, document)
            file_hash = file_sha256(temp_path)
            previous_import = find_successful_import_by_file_hash(file_hash)
            if previous_import:
                raw_sources = previous_import.get("sources", [])
                if isinstance(raw_sources, str):
                    raw_sources = [raw_sources]
                elif not isinstance(raw_sources, list):
                    raw_sources = [raw_sources]
                previous_sources = ", ".join(normalize_text(source) for source in raw_sources[:3] if normalize_text(source))
                previous_date = normalize_text(previous_import.get("timestamp")) or "дата неизвестна"
                details = [
                    "Повторный импорт заблокирован.",
                    "",
                    f"Файл: {file_name}",
                    f"Уже импортирован: {previous_date}",
                ]
                if previous_sources:
                    details.append(f"В истории: {previous_sources}")
                safe_send("\n".join(details), reply_markup=telegram_reports_keyboard())
                schedule_finish(f"Повторный Excel из Telegram заблокирован: {file_name}")
                return

            preview = prepare_excel_import([temp_path], source_names={temp_path: source_file_name})
            errors = preview.get("errors", [])
            warnings = preview.get("warnings", [])
            new_records = preview.get("new_records", [])
            duplicate_records = preview.get("duplicate_records", [])
            source_duplicate_rows = preview.get("source_duplicate_rows_count", 0)

            if not new_records:
                lines = [
                    "Новых позиций для импорта не найдено.",
                    "",
                    f"Файл: {file_name}",
                    f"Строк в файле: {preview.get('source_rows_count', 0)}",
                    f"Повторных строк в Excel: {source_duplicate_rows}",
                    f"Повторных позиций в таблице: {len(duplicate_records)}",
                    f"Адресов получено из координат: {preview.get('geocoded_count', 0)}",
                    f"Координат без адреса: {preview.get('geocode_failed_count', 0)}",
                ]
                if duplicate_records and not errors:
                    lines.insert(0, "Файл уже загружен в Google Sheets, повторный импорт заблокирован.")
                if errors:
                    lines.extend(["", "Ошибки:", "\n".join(errors[:5])])
                if warnings:
                    lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
                safe_send("\n".join(lines), reply_markup=telegram_reports_keyboard())
                schedule_finish(f"Excel из Telegram не содержит новых позиций: {file_name}")
                return

            import_result = append_import_records(new_records)
            imported_count = import_result.get("imported", 0)
            if imported_count <= 0:
                safe_send(
                    "Новые позиции не были добавлены: все строки уже есть в Google Sheets.",
                    reply_markup=telegram_reports_keyboard(),
                )
                schedule_finish(f"Excel из Telegram не добавил новых позиций: {file_name}")
                return

            loaded = None
            refresh_note = ""
            try:
                loaded = self.fetch_sheet_data_after_import()
            except Exception:
                logging.exception("Telegram: Excel импортирован, но список заказов не обновился")
                refresh_note = f"Список в окне {APP_NAME} не обновился автоматически. Нажмите «Обновить»."

            imported_blocks = sum(parse_int_value(record.get("Кол-во блок")) for record in new_records)
            lines = [
                f"{APP_NAME}: Excel импортирован из Telegram",
                "",
                f"Документ: {file_name}",
                f"Позиций загружено: {imported_count}",
                f"Повторно пропущено: {import_result.get('duplicates', 0)}",
                f"План КИЗ: {imported_blocks}",
                f"Адресов получено из координат: {preview.get('geocoded_count', 0)}",
                f"Координат без адреса: {preview.get('geocode_failed_count', 0)}",
                "",
                "Документ доступен в разделе «Документы по импорту».",
            ]
            if warnings:
                lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
            if errors:
                lines.extend(["", "Ошибки в отдельных строках:", "\n".join(errors[:5])])
            if refresh_note:
                lines.extend(["", refresh_note])
            safe_send("\n".join(lines), reply_markup=telegram_reports_keyboard())
            schedule_finish(f"✅ Excel импортирован из Telegram: {file_name}", loaded=loaded)
        except Exception as exc:
            logging.exception("Telegram: не удалось импортировать Excel-файл")
            safe_send(
                "Не удалось импортировать Excel-файл.\n\n"
                f"Файл: {file_name}\n"
                f"Причина: {exc}\n\n"
                f"Подробности записаны в лог {APP_NAME}.",
                reply_markup=telegram_reports_keyboard(),
            )
            try:
                log_path = create_today_log_file()
                send_telegram_document_to_chat(log_path, chat_id, f"{APP_NAME}: лог ошибки импорта Excel", token)
            except Exception:
                logging.exception("Telegram: не удалось отправить лог ошибки импорта")
            schedule_finish(f"Ошибка импорта Excel из Telegram: {file_name}")
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if not finish_scheduled:
                schedule_finish(f"Импорт Excel из Telegram завершён: {file_name}")

    def handle_telegram_message(self, message, settings, token):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = normalize_text(chat.get("id"))
        sender_id = normalize_text(sender.get("id"))
        if not (telegram_chat_is_authorized(chat_id, settings) or telegram_chat_is_authorized(sender_id, settings)):
            logging.warning("Telegram: отказано неизвестному chat_id=%s sender_id=%s", chat_id, sender_id)
            return

        if message.get("document"):
            self.handle_telegram_document_message(message["document"], chat_id or sender_id, token)
            return

        self.send_telegram_menu_to_chat(chat_id or sender_id, token)

    def handle_telegram_callback(self, callback_query, settings, token):
        query_id = callback_query.get("id")
        sender = callback_query.get("from") or {}
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = normalize_text(chat.get("id") or sender.get("id"))
        sender_id = normalize_text(sender.get("id"))

        if not (telegram_chat_is_authorized(chat_id, settings) or telegram_chat_is_authorized(sender_id, settings)):
            try:
                answer_telegram_callback_query(token, query_id, "Нет доступа")
            except Exception:
                logging.exception("Telegram: не удалось ответить на запрещенный callback")
            logging.warning("Telegram: запрещенный callback chat_id=%s sender_id=%s", chat_id, sender_id)
            return

        data = normalize_text(callback_query.get("data"))
        try:
            if data == TELEGRAM_CALLBACK_TODAY_SCANS:
                answer_telegram_callback_query(token, query_id, "Готовлю отчёт...")
                self.send_today_scan_report_to_chat(chat_id, token)
            elif data == TELEGRAM_CALLBACK_DOCUMENTS:
                answer_telegram_callback_query(token, query_id, "Открываю документы...")
                self.send_document_list_to_chat(chat_id, token)
            elif data.startswith(TELEGRAM_CALLBACK_DOCUMENT_PREFIX):
                answer_telegram_callback_query(token, query_id, "Готовлю документ...")
                self.send_document_report_to_chat(
                    chat_id,
                    token,
                    data[len(TELEGRAM_CALLBACK_DOCUMENT_PREFIX):],
                )
            elif data == TELEGRAM_CALLBACK_TODAY_LOG:
                answer_telegram_callback_query(token, query_id, "Готовлю лог...")
                self.send_today_log_to_chat(chat_id, token)
            else:
                answer_telegram_callback_query(token, query_id, "Открываю меню")
                self.send_telegram_menu_to_chat(chat_id, token)
        except Exception as exc:
            logging.exception("Telegram: не удалось обработать команду")
            try:
                send_telegram_message_to_chat(
                    chat_id,
                    f"Не удалось выполнить команду: {exc}",
                    token,
                    reply_markup=telegram_reports_keyboard(),
                )
            except Exception:
                logging.exception("Telegram: не удалось отправить ошибку команды")

    def process_telegram_updates(self, settings):
        token = normalize_text(settings.get("bot_token"))

        # Общий last_update_id из Google Sheets (лист _TakSklad_System, строка 3).
        # Берём максимум между общим и локальным: общий защищает от двойной
        # обработки на двух компах, локальный — fallback, если Google недоступен.
        local_state = load_telegram_state()
        local_last_id = parse_int_value(local_state.get("last_update_id"))
        shared_last_id = 0
        shared_state_available = False
        try:
            shared = read_shared_telegram_state()
            shared_last_id = parse_int_value(shared.get("last_update_id"))
            shared_state_available = True
        except Exception as exc:
            logging.info("Telegram: общий state недоступен, использую локальный (%s)", exc)
        last_update_id = max(local_last_id, shared_last_id)

        updates = fetch_telegram_updates(token, offset=last_update_id + 1 if last_update_id else None)
        max_update_id = last_update_id

        for update in updates:
            update_id = parse_int_value(update.get("update_id"))
            if update_id and update_id <= last_update_id:
                # Этот апдейт уже был обработан другим компьютером — пропускаем.
                continue
            if update_id:
                max_update_id = max(max_update_id, update_id)
            if update.get("callback_query"):
                self.handle_telegram_callback(update["callback_query"], settings, token)
            elif update.get("message"):
                self.handle_telegram_message(update["message"], settings, token)

        if max_update_id != last_update_id:
            update_telegram_state({
                "last_update_id": max_update_id,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if shared_state_available:
                try:
                    write_shared_telegram_state(
                        max_update_id,
                        owner_label=self.telegram_lock_owner_label,
                    )
                except Exception:
                    logging.exception("Telegram: не удалось записать общий state в Google Sheets")

    def ensure_telegram_poll_lock(self, settings):
        if not telegram_single_listener_lock_enabled(settings):
            return True

        now_ts = time.time()
        if (
            self.telegram_lock_owned_until > now_ts
            and now_ts - self.telegram_lock_checked_at < TELEGRAM_LOCK_REFRESH_SECONDS
        ):
            return True

        try:
            result = acquire_telegram_poll_lock(
                self.telegram_lock_owner_id,
                self.telegram_lock_owner_label,
                now_ts=now_ts,
            )
        except Exception as exc:
            self.telegram_lock_owned_until = 0
            logging.info("Telegram: lock не получен, опрос пропущен: %s", exc)
            return False

        self.telegram_lock_checked_at = now_ts
        if result.get("acquired"):
            if self.telegram_lock_owned_until <= now_ts:
                logging.info("Telegram: lock получен этим компьютером: %s", self.telegram_lock_owner_label)
            self.telegram_lock_owned_until = now_ts + TELEGRAM_LOCK_TTL_SECONDS
            return True

        self.telegram_lock_owned_until = 0
        if now_ts - self.telegram_lock_skip_logged_at > 60:
            logging.info(
                "Telegram: опрос выполняет другой компьютер: %s",
                result.get("owner_label") or result.get("owner_id") or "неизвестно",
            )
            self.telegram_lock_skip_logged_at = now_ts
        return False

    def poll_telegram_bot_async(self):
        settings = load_telegram_settings()
        polling_enabled = desktop_telegram_polling_enabled(settings)
        delay_ms = 5000 if polling_enabled else 15000
        if self.telegram_poll_running:
            self.after(delay_ms, self.poll_telegram_bot_async)
            return
        if not polling_enabled:
            self.after(delay_ms, self.poll_telegram_bot_async)
            return

        self.telegram_poll_running = True

        def worker():
            next_delay_ms = delay_ms
            try:
                if not self.ensure_telegram_poll_lock(settings):
                    next_delay_ms = TELEGRAM_LOCK_RETRY_SECONDS * 1000
                    return
                self.process_telegram_updates(settings)
            except Exception:
                logging.exception("Telegram: не удалось проверить команды бота")
            finally:
                def finish(delay_ms=next_delay_ms):
                    self.telegram_poll_running = False
                    self.after(delay_ms, self.poll_telegram_bot_async)

                try:
                    self.after(0, finish)
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()
