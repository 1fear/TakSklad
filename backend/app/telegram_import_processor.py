import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from .db import SessionLocal
from .event_queue_service import reset_stale_processing_events
from .excel_importer import ExcelDateConflictError, excel_file_to_import_payload
from .models import AuditLog, Incident, PendingEvent
from .telegram_clients import ExternalTimeoutError, TelegramProcessorDelegate
from .telegram_common import normalize_text, parse_date_from_text, parse_int
from .telegram_import_messages import (
    safe_telegram_spreadsheet_filename,
    telegram_import_failure_message,
    telegram_import_unconfirmed_message,
)


TELEGRAM_EXCEL_IMPORT_EVENT_TYPE = "telegram_excel_import"
TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS = "waiting_shipment_date"
TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS = "waiting_date_choice"
TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES = ("pending",)
TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX = "excel_date:use_excel:"
TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX = "excel_date:cancel:"


def telegram_inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}


def ensure_telegram_import_event_incident(db, event, error):
    if event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
        return None
    existing = db.execute(
        select(Incident).where(Incident.pending_event_id == event.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    payload = dict(event.payload or {})
    document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    file_name = safe_telegram_spreadsheet_filename(
        normalize_text(payload.get("file_name")) or normalize_text(document.get("file_name"))
    ) or "telegram_import.xlsx"
    incident = Incident(
        source="telegram_import",
        severity="critical",
        status="open",
        title="Telegram Excel import failed",
        message=normalize_text(error),
        entity_type="pending_event",
        entity_id=str(event.id),
        pending_event_id=event.id,
        raw_payload={
            "event_type": event.event_type,
            "event_status": event.status,
            "file_name": file_name,
            "attempts": int(event.attempts or 0),
            "error": normalize_text(error),
        },
    )
    db.add(incident)
    db.add(AuditLog(
        action="telegram_import_incident_created",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "event_type": event.event_type,
            "status": event.status,
            "file_name": file_name,
            "attempts": int(event.attempts or 0),
        },
    ))
    return incident


def find_existing_telegram_import_event(db, document, update_id=None):
    file_id = normalize_text((document or {}).get("file_id"))
    update_id = normalize_text(update_id)
    if not file_id and not update_id:
        return None
    candidates = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_((
            "pending",
            "processing",
            TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
            TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS,
            "completed",
            "failed",
        )))
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    ).scalars().all()
    for event in candidates:
        payload = event.payload or {}
        payload_document = payload.get("document") or {}
        if update_id and normalize_text(payload.get("update_id")) == update_id:
            return event
        if file_id and normalize_text(payload_document.get("file_id")) == file_id:
            return event
    return None


def telegram_import_date_choice_keyboard(event_id, excel_date):
    event_id = normalize_text(event_id)
    return telegram_inline_keyboard([
        [{
            "text": f"Использовать дату Excel: {excel_date}",
            "callback_data": f"{TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX}{event_id}",
        }],
        [{
            "text": "Отменить импорт",
            "callback_data": f"{TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX}{event_id}",
        }],
    ])


class TelegramImportProcessor(TelegramProcessorDelegate):
    def __init__(self, *, ports=None, owner=None, **port_dependencies):
        TelegramProcessorDelegate.__init__(self, ports=ports, owner=owner, **port_dependencies)

    def _import_session_factory(self):
        return getattr(self, "session_factory", None) or SessionLocal

    def _excel_import_parser(self):
        return getattr(self, "excel_import_parser", None) or excel_file_to_import_payload

    def take_waiting_telegram_import_for_date(self, chat_id):
        with self._import_session_factory()() as db:
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
                .where(PendingEvent.status == TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS)
                .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
            )
            events = db.execute(stmt).scalars().all()
            for event in events:
                payload = event.payload or {}
                if normalize_text(payload.get("chat_id")) == normalize_text(chat_id):
                    return str(event.id), dict(payload)
        return "", {}

    def cancel_waiting_telegram_imports_for_chat(self, db, chat_id):
        normalized_chat_id = normalize_text(chat_id)
        events = db.execute(
            select(PendingEvent)
            .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
            .where(PendingEvent.status.in_((
                TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
                TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS,
            )))
        ).scalars().all()
        cancelled = 0
        for event in events:
            payload = dict(event.payload or {})
            if normalize_text(payload.get("chat_id")) != normalized_chat_id:
                continue
            payload["superseded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payload["superseded_reason"] = "new_telegram_excel_file"
            event.payload = payload
            event.status = "cancelled"
            event.last_error = "superseded_by_new_telegram_excel_file"
            cancelled += 1
        return cancelled

    def confirm_waiting_telegram_import_shipment_date(self, chat_id, shipment_date):
        if not self.ensure_admin_chat(chat_id):
            return False
        parsed_date = parse_date_from_text(shipment_date)
        event_id, payload = self.take_waiting_telegram_import_for_date(chat_id)
        if not event_id:
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        if not parsed_date:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Ожидаю дату отгрузки для Excel-файла.",
                    "",
                    f"Файл: {file_name}",
                    "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
                    "Пример: 09.06.2026",
                ]),
            )
            return True

        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            self.safe_send_message(chat_id, "Не удалось найти ожидающий импорт. Отправьте Excel-файл заново.")
            return True

        with self._import_session_factory()() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None or event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
                self.safe_send_message(chat_id, "Ожидающий импорт не найден. Отправьте Excel-файл заново.")
                return True
            if event.status != TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS:
                self.safe_send_message(chat_id, "Этот Excel-файл уже обработан или находится в очереди.")
                return True
            payload = dict(event.payload or {})
            payload["shipment_date"] = parsed_date
            payload["shipment_date_source"] = "telegram_manual_input"
            payload["shipment_date_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            event.payload = payload
            event.status = "pending"
            event.last_error = ""
            db.commit()

        self.safe_send_message(
            chat_id,
            "\n".join([
                "Дата принята. Excel-файл поставлен в очередь импорта.",
                "",
                f"Файл: {file_name}",
                f"Дата отгрузки: {parsed_date}",
            ]),
        )
        self.process_queued_telegram_imports()
        return True

    def parse_telegram_import_event_id(self, event_id):
        try:
            return uuid.UUID(normalize_text(event_id))
        except (TypeError, ValueError):
            return None

    def send_telegram_import_date_conflict_choice(self, chat_id, file_name, event_id, conflict):
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Найден конфликт дат при импорте Excel.",
                "",
                f"Файл: {file_name}",
                f"Дата в Excel: {conflict.excel_date}",
                f"Дата в Telegram: {conflict.telegram_date}",
                "",
                "Заказы и заявки SkladBot ещё не созданы. Выберите дату Excel или отмените импорт.",
            ]),
            reply_markup=telegram_import_date_choice_keyboard(event_id, conflict.excel_date),
        )

    def mark_telegram_import_waiting_date_choice(self, event_id, conflict):
        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            return False
        with self._import_session_factory()() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None:
                return False
            payload = dict(event.payload or {})
            payload["date_conflict"] = {
                "telegram_date": conflict.telegram_date,
                "excel_date": conflict.excel_date,
                "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            event.payload = payload
            event.status = TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS
            event.last_error = "date_choice_required"
            db.commit()
            return True

    def resolve_telegram_import_date_choice(self, chat_id, event_id, action):
        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            return False, {}, "Кнопка устарела: некорректный ID импорта."
        with self._import_session_factory()() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None or event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
                return False, {}, "Кнопка устарела: импорт не найден."
            payload = dict(event.payload or {})
            if normalize_text(payload.get("chat_id")) != normalize_text(chat_id):
                return False, {}, "Нет доступа к этому импорту."
            if event.status != TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS:
                return False, payload, "Этот импорт уже обработан или отменён."

            resolution = {
                "action": action,
                "resolved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if action == "use_excel":
                payload["shipment_date"] = ""
                event.status = "pending"
                event.last_error = ""
            elif action == "cancel":
                event.status = "cancelled"
                event.last_error = "cancelled_by_user"
            else:
                return False, payload, "Неизвестный выбор даты."
            payload["date_choice_resolution"] = resolution
            event.payload = payload
            db.commit()
            return True, payload, ""

    def confirm_telegram_import_excel_date(self, chat_id, event_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        success, payload, error = self.resolve_telegram_import_date_choice(chat_id, event_id, "use_excel")
        if not success:
            self.safe_send_message(chat_id, error)
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        excel_date = normalize_text((payload.get("date_conflict") or {}).get("excel_date"))
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Принято. Импорт будет выполнен по дате из Excel.",
                "",
                f"Файл: {file_name}",
                f"Дата отгрузки: {excel_date or 'из Excel'}",
            ]),
        )
        self.process_queued_telegram_imports()
        return True

    def cancel_telegram_import_date_choice(self, chat_id, event_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        success, payload, error = self.resolve_telegram_import_date_choice(chat_id, event_id, "cancel")
        if not success:
            self.safe_send_message(chat_id, error)
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Импорт отменён.",
                "",
                f"Файл: {file_name}",
                "Заказы и заявки SkladBot не созданы.",
            ]),
        )
        return True

    def import_telegram_document(self, chat_id, document, shipment_date="", event_id=None):
        file_name = safe_telegram_spreadsheet_filename(document.get("file_name"))
        if not file_name:
            self.safe_send_message(chat_id, "Файл не импортирован. Отправьте Excel-файл в формате .xlsx или .xlsm.")
            return False, "unsafe_filename"

        document = {**document, "file_name": file_name}

        suffix = Path(file_name).suffix.lower() or ".xlsx"
        temp_file = tempfile.NamedTemporaryFile(prefix="taksklad_telegram_import_", suffix=suffix, delete=False)
        temp_path = temp_file.name
        temp_file.close()

        try:
            self.safe_send_message(chat_id, f"Начинаю импорт Excel-файла из очереди: {file_name}")
            self.download_telegram_document(document, temp_path)
            import_payload = self._excel_import_parser()(
                temp_path,
                file_name=file_name,
                source="telegram",
                shipment_date=shipment_date,
                force_shipment_date=bool(parse_date_from_text(shipment_date)),
            )
            meta = import_payload.pop("meta", {})
            rows = import_payload.get("rows") or []
            imported_blocks = sum(parse_int(row.get("Кол-во блок")) for row in rows if isinstance(row, dict))
            import_payload["telegram_chat_id"] = normalize_text(chat_id)
            if event_id:
                import_payload["telegram_event_id"] = normalize_text(event_id)
            recovered_after_timeout = False
            try:
                result = self.backend_post("/api/v1/imports", import_payload)
            except ExternalTimeoutError as exc:
                recovered = None
                try:
                    recovered = self.find_backend_import_by_telegram_event_id(event_id)
                except Exception:
                    logging.exception("Telegram worker: import timeout read-back failed")
                if not recovered:
                    reason = f"backend import response timeout: {exc}"
                    self.safe_send_message(chat_id, telegram_import_unconfirmed_message(file_name, reason))
                    return False, reason
                result = recovered
                recovered_after_timeout = True
            result_status = normalize_text(result.get("status"))
            if result_status == "failed":
                errors = result.get("errors") or []
                reason = normalize_text(errors[0] if errors else "") or "backend import status failed"
                self.safe_send_message(chat_id, telegram_import_failure_message(file_name, reason))
                return False, reason
            warnings = meta.get("warnings") or []
            lines = [
                "TakSklad: Excel импортирован через Telegram",
                "",
                f"Файл: {file_name}",
                f"Строк в файле: {meta.get('source_rows_count', 0)}",
                f"Строк отправлено в backend: {len(rows)}",
                f"Блоков импортировано: {imported_blocks}",
                f"Дата отгрузки: {meta.get('shipment_date') or shipment_date or 'не задана'}",
                f"Позиции добавлены: {result.get('items_created', 0)}",
                f"Заказы добавлены: {result.get('orders_created', 0)}",
                f"Адреса в backend обновлены: {result.get('backend_address_updates', 0)}",
                f"Повторы пропущены: {result.get('duplicate_rows', 0)}",
                f"Ошибочные строки: {result.get('invalid_rows', 0)}",
                f"Статус: {result.get('status', '')}",
            ]
            google_sheets_status = normalize_text(result.get("google_sheets_status"))
            if google_sheets_status == "completed":
                lines.append(
                    f"Google Sheets: записано {result.get('google_sheets_imported', 0)}, "
                    f"повторы {result.get('google_sheets_duplicates', 0)}, "
                    f"адреса обновлены {result.get('google_sheets_updated', 0)}"
                )
            elif google_sheets_status == "skipped":
                lines.append("Google Sheets: новых строк нет")
            elif google_sheets_status == "disabled":
                lines.append("Google Sheets: экспорт отключён на backend")
            elif google_sheets_status == "error":
                error_text = normalize_text(result.get("google_sheets_error")) or "подробности в логе backend"
                lines.append(f"Google Sheets: ошибка, строки не записаны ({error_text})")
            errors = result.get("errors") or []
            if warnings:
                lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
            if errors:
                lines.extend(["", "Ошибки:", "\n".join(errors[:5])])
            if recovered_after_timeout:
                lines.extend(["", "Ответ backend потерялся по timeout, результат подтверждён через историю импортов."])
            self.safe_send_message(chat_id, "\n".join(lines))
            return True, ""
        except ExcelDateConflictError as exc:
            logging.warning("Telegram worker: Excel import date conflict: %s", exc)
            if event_id:
                self.mark_telegram_import_waiting_date_choice(event_id, exc)
                self.send_telegram_import_date_conflict_choice(chat_id, file_name, event_id, exc)
                return None, "date_choice_required"
            self.safe_send_message(
                chat_id,
                telegram_import_failure_message(file_name, exc),
            )
            return False, str(exc)
        except Exception as exc:
            logging.exception("Telegram worker: Excel import failed")
            self.safe_send_message(chat_id, telegram_import_failure_message(file_name, exc))
            return False, str(exc)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def find_backend_import_by_telegram_event_id(self, event_id):
        event_id = normalize_text(event_id)
        if not event_id:
            return None
        payload = self.backend_get("/api/v1/imports")
        imports = payload if isinstance(payload, list) else []
        for item in imports:
            if not isinstance(item, dict):
                continue
            raw_payload = item.get("raw_payload") or {}
            if normalize_text(raw_payload.get("telegram_event_id")) != event_id:
                continue
            google_sheets = raw_payload.get("google_sheets") if isinstance(raw_payload.get("google_sheets"), dict) else {}
            return {
                "id": item.get("id") or "",
                "source": item.get("source") or raw_payload.get("source") or "telegram",
                "status": item.get("status") or raw_payload.get("status") or "",
                "rows_total": item.get("rows_total", 0),
                "rows_imported": item.get("rows_imported", 0),
                "orders_created": raw_payload.get("orders_created", 0),
                "items_created": raw_payload.get("items_created", item.get("rows_imported", 0)),
                "duplicate_rows": raw_payload.get("duplicate_rows", 0),
                "invalid_rows": raw_payload.get("invalid_rows", 0),
                "errors": raw_payload.get("errors") or [],
                "backend_address_updates": raw_payload.get("backend_address_updates", 0),
                "google_sheets_status": google_sheets.get("status", ""),
                "google_sheets_imported": google_sheets.get("imported", 0),
                "google_sheets_duplicates": google_sheets.get("duplicates", 0),
                "google_sheets_updated": google_sheets.get("updated", 0),
                "google_sheets_error": google_sheets.get("error", ""),
            }
        return None

    def enqueue_telegram_document(self, chat_id, document, update_id=None, shipment_date=""):
        if not self.ensure_admin_chat(chat_id):
            return False
        file_name = safe_telegram_spreadsheet_filename(document.get("file_name"))
        if not file_name:
            self.safe_send_message(chat_id, "Файл не импортирован. Отправьте Excel-файл в формате .xlsx или .xlsm.")
            return False

        document = {**document, "file_name": file_name}

        with self._import_session_factory()() as db:
            existing_event = find_existing_telegram_import_event(db, document, update_id)
            if existing_event is not None:
                if existing_event.status == "failed":
                    payload = dict(existing_event.payload or {})
                    payload["shipment_date"] = ""
                    payload["shipment_date_source"] = ""
                    payload["requeued_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    existing_event.payload = payload
                    existing_event.status = TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS
                    existing_event.last_error = ""
                    existing_event.attempts = 0
                    db.commit()
                if existing_event.status == TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS:
                    self.safe_send_message(
                        chat_id,
                        "\n".join([
                            "Excel-файл уже получен и ждёт дату отгрузки.",
                            "",
                            f"Файл: {file_name}",
                            "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
                            "Пример: 09.06.2026",
                        ]),
                    )
                    return True
                if existing_event.status == TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS:
                    payload = existing_event.payload or {}
                    conflict_payload = payload.get("date_conflict") or {}
                    telegram_date = normalize_text(conflict_payload.get("telegram_date"))
                    excel_date = normalize_text(conflict_payload.get("excel_date"))
                    if telegram_date and excel_date:
                        conflict = ExcelDateConflictError(telegram_date, excel_date)
                        self.send_telegram_import_date_conflict_choice(
                            chat_id,
                            file_name,
                            str(existing_event.id),
                            conflict,
                        )
                        return True
                self.safe_send_message(
                    chat_id,
                    "\n".join([
                        "Excel-файл уже есть в очереди импорта.",
                        "",
                        f"Файл: {file_name}",
                        "Дата отгрузки: уже задана или импорт завершён",
                    ]),
                )
                return True

            self.cancel_waiting_telegram_imports_for_chat(db, chat_id)
            event = PendingEvent(
                event_type=TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                status=TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
                payload={
                    "chat_id": normalize_text(chat_id),
                    "document": document,
                    "file_name": file_name,
                    "update_id": update_id,
                    "shipment_date": "",
                    "shipment_date_source": "",
                    "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            db.add(event)
            db.commit()

        self.safe_send_message(
            chat_id,
            "\n".join([
                "Excel-файл получен.",
                "",
                f"Файл: {file_name}",
                "Укажите дату отгрузки одним сообщением в формате ДД.ММ.ГГГГ.",
                "Пример: 09.06.2026",
                "Заказы и заявки SkladBot не будут созданы до ввода даты.",
            ]),
        )
        return True

    def take_next_telegram_import_event(self):
        with self._import_session_factory()() as db:
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
                .where(PendingEvent.status.in_(TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES))
                .order_by(PendingEvent.created_at, PendingEvent.id)
            )
            if db.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            event = db.execute(stmt).scalars().first()
            if event is None:
                return None

            event.status = "processing"
            event.attempts = (event.attempts or 0) + 1
            payload = event.payload or {}
            event_id = event.id
            db.commit()
            return {"id": event_id, "payload": payload}

    def finish_telegram_import_event(self, event_id, success, error=""):
        with self._import_session_factory()() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            event.status = "completed" if success else "failed"
            event.last_error = "" if success else normalize_text(error)
            if not success:
                ensure_telegram_import_event_incident(db, event, error)
            db.commit()

    def reset_stale_telegram_import_events(self):
        with self._import_session_factory()() as db:
            return reset_stale_processing_events(
                db,
                event_types=(TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,),
                action="telegram_excel_import_stale_reset",
                last_error="stale Telegram Excel import reset",
            )

    def process_queued_telegram_imports(self):
        self.reset_stale_telegram_import_events()
        processed = 0
        while True:
            event = self.take_next_telegram_import_event()
            if not event:
                break
            payload = event.get("payload") or {}
            chat_id = normalize_text(payload.get("chat_id"))
            document = payload.get("document") or {}
            if not self.is_admin_chat(chat_id):
                self.finish_telegram_import_event(
                    event["id"],
                    False,
                    "telegram import chat is not authorized",
                )
                processed += 1
                continue
            result = self.import_telegram_document(
                chat_id,
                document,
                shipment_date=payload.get("shipment_date") or "",
                event_id=str(event.get("id") or ""),
            )
            success, error = result if isinstance(result, tuple) else (False, "telegram_import_failed")
            if success is None:
                processed += 1
                continue
            self.finish_telegram_import_event(event["id"], success, error)
            processed += 1
        return processed
