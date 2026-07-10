import logging
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .event_queue_service import reset_stale_processing_events
from .models import AuditLog, PendingEvent
from .telegram_clients import TelegramProcessorDelegate
from .telegram_common import display_date, normalize_text, parse_date_from_text, parse_int, text_matches
from .telegram_manual_support import (
    build_manual_import_payload,
    manual_address_and_coordinates,
    manual_order_summary,
    order_planned_blocks,
    order_scanned_blocks,
    telegram_manual_add_next_keyboard,
    telegram_manual_delete_confirm_keyboard,
    telegram_manual_delete_keyboard,
    telegram_manual_menu_keyboard,
    telegram_manual_payment_keyboard,
    telegram_manual_product_keyboard,
)
from .telegram_report_processor import backend_failure_message, backend_http_error_detail


TELEGRAM_MANUAL_CALLBACK_PREFIX = "manual:"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
TELEGRAM_NOTIFICATION_ACTIVE_STATUSES = ("pending", "failed")
TELEGRAM_CHAT_STATE_EVENT_PREFIX = "telegram_chat_state:"
TELEGRAM_MANUAL_PRODUCTS = {
    "brown_op": "Chapman Brown OP 20",
    "brown_ssl": "Chapman Brown SSL 100`20",
    "red_op": "Chapman RED OP 20",
    "red_ssl": "Chapman RED SSL 100 20",
    "gold_ssl": "Chapman Gold SSL 100`20",
    "green_op": "Chapman Green OP 20",
}
TELEGRAM_MANUAL_PAYMENT_TYPES = {
    "terminal": "Терминал",
    "transfer": "Перечисление",
}


class TelegramAdminProcessor(TelegramProcessorDelegate):
    def __init__(self, *, ports=None, owner=None, **port_dependencies):
        TelegramProcessorDelegate.__init__(self, ports=ports, owner=owner, **port_dependencies)

    def _admin_session_factory(self):
        return getattr(self, "session_factory", None) or SessionLocal

    def chat_state_event_type(self, chat_id):
        return f"{TELEGRAM_CHAT_STATE_EVENT_PREFIX}{chat_id}"

    def is_admin_chat(self, chat_id):
        chat_id = str(chat_id)
        return self.is_allowed_chat(chat_id) and chat_id in getattr(self, "admin_chat_ids", set())

    def is_allowed_chat(self, chat_id):
        return str(chat_id) in getattr(self, "allowed_chat_ids", set())

    def ensure_admin_chat(self, chat_id):
        if self.is_admin_chat(chat_id):
            return True
        if self.is_allowed_chat(chat_id):
            self.send_message(chat_id, "Команда доступна только администратору.")
        logging.warning("Telegram worker denied admin command")
        return False

    def get_chat_state(self, chat_id):
        with self._admin_session_factory()() as db:
            state = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == self.chat_state_event_type(chat_id))
            ).scalars().first()
            return dict(state.payload or {}) if state else {}

    def save_chat_state(self, chat_id, payload):
        with self._admin_session_factory()() as db:
            state = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == self.chat_state_event_type(chat_id))
            ).scalars().first()
            if state is None:
                state = PendingEvent(event_type=self.chat_state_event_type(chat_id), status="active", payload={})
                db.add(state)
            state.payload = payload
            db.commit()

    def get_chat_shipment_date(self, chat_id):
        return normalize_text(self.get_chat_state(chat_id).get("shipment_date"))

    def set_chat_shipment_date(self, chat_id, shipment_date):
        state = self.get_chat_state(chat_id)
        state["shipment_date"] = shipment_date
        state["shipment_date_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_chat_state(chat_id, state)

    def show_manual_menu(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Ручное управление TakSklad",
                "",
                "Удалять можно только активные заказы без сканов КИЗов.",
                "Если склад уже начал обрабатывать заказ, бот его не удалит.",
            ]),
            reply_markup=telegram_manual_menu_keyboard(),
        )
        return True

    def clear_manual_flow(self, chat_id):
        state = self.get_chat_state(chat_id)
        state["manual_flow"] = {}
        self.save_chat_state(chat_id, state)
        cache = getattr(self, "manual_flow_cache", None)
        if isinstance(cache, dict):
            cache[str(chat_id)] = {}

    def save_manual_flow(self, chat_id, flow):
        state = self.get_chat_state(chat_id)
        state["manual_flow"] = flow or {}
        self.save_chat_state(chat_id, state)
        cache = getattr(self, "manual_flow_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.manual_flow_cache = cache
        cache[str(chat_id)] = flow or {}

    def start_manual_add_order(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        flow = {
            "mode": "add_order",
            "step": "order_date",
            "data": {
                "manual_id": str(uuid.uuid4()),
                "items": [],
            },
        }
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(
            chat_id,
            "Введите дату отгрузки в формате ДД.ММ.ГГГГ.",
        )
        return True

    def handle_manual_text(self, chat_id, text):
        cache = getattr(self, "manual_flow_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.manual_flow_cache = cache
        flow = cache.get(str(chat_id)) or {}
        if not flow and self.configured:
            try:
                state = self.get_chat_state(chat_id)
            except Exception:
                logging.warning("Telegram worker: failed to load manual flow state", exc_info=True)
                state = {}
            flow = (state.get("manual_flow") if isinstance(state, dict) else {}) or {}
            if flow:
                cache[str(chat_id)] = flow
        if not flow:
            return False
        if flow.get("mode") == "add_order" and not self.ensure_admin_chat(chat_id):
            self.clear_manual_flow(chat_id)
            return True
        if text_matches(text, "/cancel", "отмена", "cancel"):
            self.clear_manual_flow(chat_id)
            self.safe_send_message(chat_id, "Ручное действие отменено.")
            return True
        if flow.get("mode") == "add_order":
            return self.handle_manual_add_text(chat_id, text, flow)
        self.clear_manual_flow(chat_id)
        self.safe_send_message(chat_id, "Ручное действие устарело. Начните заново через меню.")
        return True

    def handle_manual_add_text(self, chat_id, text, flow):
        data = flow.setdefault("data", {})
        step = normalize_text(flow.get("step"))
        if step == "order_date":
            order_date = parse_date_from_text(text)
            if not order_date:
                self.safe_send_message(chat_id, "Дата не распознана. Введите дату в формате ДД.ММ.ГГГГ.")
                return True
            data["order_date"] = order_date
            flow["step"] = "payment_type"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Выберите тип оплаты:", reply_markup=telegram_manual_payment_keyboard())
            return True
        if step == "payment_type":
            payment_type = self.manual_payment_type_from_text(text)
            if not payment_type:
                self.safe_send_message(chat_id, "Выберите тип оплаты кнопкой.")
                return True
            data["payment_type"] = payment_type
            flow["step"] = "client"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите юрлицо клиента.")
            return True
        if step == "client":
            if not text:
                self.safe_send_message(chat_id, "Юрлицо не может быть пустым.")
                return True
            data["client"] = text
            flow["step"] = "address"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите адрес или координаты. Если самовывоз, напишите: Самовывоз со склада.")
            return True
        if step == "address":
            if not text:
                self.safe_send_message(chat_id, "Адрес не может быть пустым.")
                return True
            address, coordinates = manual_address_and_coordinates(text)
            data["address"] = address
            data["coordinates"] = coordinates
            flow["step"] = "representative"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите торгового представителя.")
            return True
        if step == "representative":
            if not text:
                self.safe_send_message(chat_id, "Торговый представитель не может быть пустым.")
                return True
            data["representative"] = text
            flow["step"] = "product"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
            return True
        if step == "blocks":
            blocks = parse_int(text)
            if blocks <= 0:
                self.safe_send_message(chat_id, "Введите количество блоков числом больше 0.")
                return True
            product_key = normalize_text(data.get("selected_product_key"))
            product = TELEGRAM_MANUAL_PRODUCTS.get(product_key)
            if not product:
                flow["step"] = "product"
                self.save_manual_flow(chat_id, flow)
                self.safe_send_message(chat_id, "SKU не выбран. Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
                return True
            data.setdefault("items", []).append({"product_key": product_key, "product": product, "blocks": blocks})
            data.pop("selected_product_key", None)
            flow["step"] = "review"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, manual_order_summary(flow), reply_markup=telegram_manual_add_next_keyboard())
            return True
        self.safe_send_message(chat_id, "Используйте кнопки под сообщением.")
        return True

    def manual_payment_type_from_text(self, value):
        text = normalize_text(value).casefold()
        for key, label in TELEGRAM_MANUAL_PAYMENT_TYPES.items():
            if text in {key.casefold(), label.casefold()}:
                return label
        return ""

    def set_manual_payment_type(self, chat_id, key):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order" or flow.get("step") != "payment_type":
            self.safe_send_message(chat_id, "Выбор типа оплаты устарел. Начните заново через меню.")
            return False
        payment_type = TELEGRAM_MANUAL_PAYMENT_TYPES.get(key)
        if not payment_type:
            self.safe_send_message(chat_id, "Неизвестный тип оплаты. Выберите заново.")
            return False
        flow.setdefault("data", {})["payment_type"] = payment_type
        flow["step"] = "client"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, "Введите юрлицо клиента.")
        return True

    def set_manual_product(self, chat_id, key):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order" or flow.get("step") not in {"product", "review"}:
            self.safe_send_message(chat_id, "Выбор SKU устарел. Начните заново через меню.")
            return False
        product = TELEGRAM_MANUAL_PRODUCTS.get(key)
        if not product:
            self.safe_send_message(chat_id, "Неизвестный SKU. Выберите заново.")
            return False
        flow.setdefault("data", {})["selected_product_key"] = key
        flow["step"] = "blocks"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, f"Введите количество блоков для {product}.")
        return True

    def show_manual_product_choice(self, chat_id):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order":
            self.safe_send_message(chat_id, "Ручной заказ не найден. Начните заново через меню.")
            return False
        flow["step"] = "product"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, "Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
        return True

    def create_manual_order(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        data = flow.get("data") or {}
        if flow.get("mode") != "add_order" or not data.get("items"):
            self.safe_send_message(chat_id, "В ручном заказе нет позиций. Добавьте SKU и количество.")
            return False
        required_fields = ["order_date", "payment_type", "client", "address", "representative"]
        missing = [field for field in required_fields if not normalize_text(data.get(field))]
        if missing:
            self.safe_send_message(chat_id, "Ручной заказ заполнен не полностью. Начните заново через меню.")
            return False
        payload = build_manual_import_payload(chat_id, flow)
        try:
            result = self.backend_post("/api/v1/imports", payload)
        except httpx.HTTPStatusError as exc:
            detail = backend_http_error_detail(exc)
            self.safe_send_message(chat_id, f"Не удалось создать ручной заказ: {detail or exc}")
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(chat_id, f"Не удалось создать ручной заказ: {exc.__class__.__name__}")
            return False
        self.clear_manual_flow(chat_id)
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Заказ создан в TakSklad.",
                f"Заказов добавлено: {result.get('orders_created', 0)}",
                f"Позиций добавлено: {result.get('items_created', 0)}",
                f"SkladBot: {result.get('skladbot_dry_run_status') or 'queued'}",
            ]),
        )
        return True

    def show_manual_delete_orders(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        orders = self.backend_get("/api/v1/orders/active")
        orders = orders if isinstance(orders, list) else []
        state = self.get_chat_state(chat_id)
        state["manual_delete_orders"] = orders[:20]
        self.save_chat_state(chat_id, state)
        if not orders:
            self.safe_send_message(chat_id, "Активных заказов для удаления нет.")
            return True
        lines = [
            "Выберите активный заказ для удаления.",
            "",
            "Важно: если в заказе есть хотя бы один скан КИЗа, удалить его через бот нельзя.",
        ]
        for index, order in enumerate(orders[:20], start=1):
            lines.append(
                f"{index}. {display_date(order.get('order_date')) or 'без даты'} | "
                f"{normalize_text(order.get('client')) or 'без клиента'} | "
                f"{order_scanned_blocks(order)}/{order_planned_blocks(order)} блок."
            )
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_manual_delete_keyboard(orders[:20]))
        return True

    def select_manual_delete_order(self, chat_id, index):
        state = self.get_chat_state(chat_id)
        orders = state.get("manual_delete_orders") or []
        if index < 1 or index > len(orders):
            self.safe_send_message(chat_id, "Заказ из списка не найден. Откройте список заново.")
            return False
        order = orders[index - 1]
        if order_scanned_blocks(order) > 0:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Склад уже начал обрабатывать заказ: есть сканы КИЗов.",
                    "Через Telegram удалить нельзя, чтобы не потерять данные.",
                ]),
            )
            return False
        order_id = normalize_text(order.get("id"))
        lines = [
            "Подтвердите удаление активного заказа:",
            "",
            f"Дата: {display_date(order.get('order_date')) or 'без даты'}",
            f"Клиент: {normalize_text(order.get('client')) or 'без клиента'}",
            f"SkladBot: {normalize_text(order.get('skladbot_request_number')) or 'нет'}",
            f"Блоков: {order_planned_blocks(order)}",
            "",
            "Из SkladBot бот удалить не может. Если заявка там создана, её нужно удалить вручную.",
        ]
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_manual_delete_confirm_keyboard(order_id))
        return True

    def confirm_manual_delete_order(self, chat_id, order_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        order_id = normalize_text(order_id)
        if not order_id:
            self.safe_send_message(chat_id, "ID заказа не найден. Откройте список заново.")
            return False
        order, error = self.find_manual_delete_order_for_confirmation(chat_id, order_id)
        if error:
            self.safe_send_message(chat_id, error)
            return False
        if order and order_scanned_blocks(order) > 0:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Склад уже начал обрабатывать заказ: есть сканы КИЗов.",
                    "Через Telegram удалить нельзя, чтобы не потерять данные.",
                ]),
            )
            return False
        payload = {
            "reason": "Удалено вручную через Telegram",
            "actor": "telegram",
            "source": "telegram",
            "idempotency_key": f"telegram:manual_delete:{chat_id}:{order_id}",
        }
        try:
            result = self.backend_post(f"/api/v1/admin/orders/{order_id}/delete-active", payload)
        except httpx.HTTPStatusError as exc:
            detail = backend_http_error_detail(exc)
            self.safe_send_message(chat_id, f"Заказ не удалён: {detail or exc}")
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(chat_id, f"Заказ не удалён: {exc.__class__.__name__}")
            return False
        state = self.get_chat_state(chat_id)
        state["manual_delete_orders"] = []
        self.save_chat_state(chat_id, state)
        lines = ["Заказ удалён из TakSklad и поставлен на удаление из Google Sheets."]
        skladbot_number = normalize_text(result.get("skladbot_request_number"))
        if skladbot_number:
            lines.append(f"В SkladBot заявка {skladbot_number} осталась, её нужно удалить вручную.")
        else:
            lines.append("SkladBot-заявки у заказа не было.")
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def find_manual_delete_order_for_confirmation(self, chat_id, order_id):
        state = self.get_chat_state(chat_id)
        orders = state.get("manual_delete_orders") or []
        for order in orders:
            if normalize_text(order.get("id")) == order_id:
                return order, ""
        try:
            active_orders = self.backend_get("/api/v1/orders/active")
        except (httpx.HTTPError, Exception) as exc:
            return None, backend_failure_message("Не удалось проверить заказ перед удалением", exc)
        for order in active_orders if isinstance(active_orders, list) else []:
            if normalize_text(order.get("id")) == order_id:
                return order, ""
        return None, "Список удаления устарел. Откройте активные заказы заново."

    def handle_manual_callback(self, chat_id, data):
        action = normalize_text(data).replace(TELEGRAM_MANUAL_CALLBACK_PREFIX, "", 1)
        if not self.ensure_admin_chat(chat_id):
            return False
        if action == "cancel":
            self.clear_manual_flow(chat_id)
            state = self.get_chat_state(chat_id)
            state["manual_delete_orders"] = []
            self.save_chat_state(chat_id, state)
            self.safe_send_message(chat_id, "Ручное действие отменено.")
            return True
        if action == "add":
            return self.start_manual_add_order(chat_id)
        if action == "delete":
            return self.show_manual_delete_orders(chat_id)
        if action.startswith("payment:"):
            return self.set_manual_payment_type(chat_id, action.split(":", 1)[1])
        if action.startswith("product:"):
            return self.set_manual_product(chat_id, action.split(":", 1)[1])
        if action == "add_more":
            return self.show_manual_product_choice(chat_id)
        if action == "create":
            return self.create_manual_order(chat_id)
        if action.startswith("delete_confirm:"):
            return self.confirm_manual_delete_order(chat_id, action.split(":", 1)[1])
        if action.startswith("delete:"):
            return self.select_manual_delete_order(chat_id, parse_int(action.split(":", 1)[1]))
        self.safe_send_message(chat_id, "Ручное действие устарело. Начните заново через меню.")
        return False

    def send_backend_diagnostics_log(self, chat_id):
        content, headers = self.backend_get_bytes("/api/v1/diagnostics/logs", params={"limit": 100})
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or "TakSklad_backend_diagnostics.txt"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption="TakSklad: критичные backend-события и ошибки очередей",
        )
        return True

    def take_next_telegram_notification_event(self):
        with self._admin_session_factory()() as db:
            if event_leases_enabled():
                owner = f"telegram-notification:{uuid.uuid4()}"
                events = claim_event_leases(
                    db,
                    event_types=(TELEGRAM_NOTIFICATION_EVENT_TYPE,),
                    owner=owner,
                    limit=1,
                )
                if not events:
                    return None
                event = events[0]
                return {"id": event.id, "payload": event.payload or {}, "lease_owner": owner}
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_NOTIFICATION_EVENT_TYPE)
                .where(PendingEvent.status.in_(TELEGRAM_NOTIFICATION_ACTIVE_STATUSES))
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

    def finish_telegram_notification_event(
        self, event_id, success, error="", failure_status="failed", lease_owner="",
    ):
        with self._admin_session_factory()() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            status = "completed" if success else normalize_text(failure_status) or "failed"
            last_error = "" if success else normalize_text(error)
            if not success and status == "blocked":
                db.add(AuditLog(
                    action="telegram_notification_blocked",
                    entity_type="pending_event",
                    entity_id=str(event.id),
                    payload={
                        "event_type": event.event_type,
                        "reason": last_error,
                        "attempts": int(event.attempts or 0),
                    },
                ))
            if lease_owner:
                finalize_event_leases(
                    db,
                    event_ids=(event.id,),
                    owner=lease_owner,
                    status=status,
                    last_error=last_error,
                    payload=event.payload or {},
                    available_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                )
            else:
                event.status = status
                event.last_error = last_error
                event.completed_at = datetime.now(timezone.utc) if status in {"completed", "blocked"} else None
                db.commit()

    def reset_stale_telegram_notification_events(self):
        if event_leases_enabled():
            return 0
        with self._admin_session_factory()() as db:
            return reset_stale_processing_events(
                db,
                event_types=(TELEGRAM_NOTIFICATION_EVENT_TYPE,),
                action="telegram_notification_stale_reset",
                last_error="stale Telegram notification reset",
            )

    def telegram_notification_targets(self, payload):
        chat_id = normalize_text((payload or {}).get("chat_id"))
        if chat_id:
            return [chat_id]
        fallback = sorted(getattr(self, "admin_chat_ids", set()) or getattr(self, "allowed_chat_ids", set()))
        return [normalize_text(value) for value in fallback if normalize_text(value)]

    def process_pending_telegram_notifications(self):
        self.reset_stale_telegram_notification_events()
        processed = 0
        while True:
            event = self.take_next_telegram_notification_event()
            if not event:
                break
            payload = event.get("payload") or {}
            lease_owner = event.get("lease_owner") or ""
            text = normalize_text(payload.get("text"))
            targets = self.telegram_notification_targets(payload)
            if not text:
                self.finish_telegram_notification_event(
                    event["id"],
                    False,
                    "telegram notification text is empty",
                    failure_status="blocked",
                    lease_owner=lease_owner,
                )
                processed += 1
                continue
            if any(not self.is_allowed_chat(chat_id) for chat_id in targets):
                self.finish_telegram_notification_event(
                    event["id"],
                    False,
                    "telegram notification target is not allowed",
                    failure_status="blocked",
                    lease_owner=lease_owner,
                )
                processed += 1
                continue
            if not targets:
                self.finish_telegram_notification_event(
                    event["id"],
                    False,
                    "telegram notification target chat is empty",
                    failure_status="blocked",
                    lease_owner=lease_owner,
                )
                processed += 1
                continue
            try:
                for chat_id in targets:
                    self.send_message(chat_id, text)
                self.finish_telegram_notification_event(event["id"], True, "", lease_owner=lease_owner)
            except Exception as exc:
                logging.exception("Telegram worker: queued notification failed")
                self.finish_telegram_notification_event(
                    event["id"], False, str(exc), lease_owner=lease_owner,
                )
            processed += 1
        return processed

    def load_offset(self):
        try:
            with self._admin_session_factory()() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                return int((state.payload or {}).get("offset") or 0) if state else 0
        except Exception:
            logging.info("Telegram worker: offset not loaded from database", exc_info=True)
            return 0

    def save_offset(self):
        try:
            with self._admin_session_factory()() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                if state is None:
                    state = PendingEvent(event_type="telegram_worker_state", status="active", payload={})
                    db.add(state)
                state.payload = {"offset": self.offset}
                db.commit()
        except Exception:
            logging.info("Telegram worker: offset not saved to database", exc_info=True)
