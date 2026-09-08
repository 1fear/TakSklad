"""Возврат по перечислению проходит только после одобрения владельцем в боте.

Заявка возврата в СкладБоте по таким заказам заводится вручную, поэтому склад
не оформляет возврат сам: запрос уходит в личный чат владельца, а решение
возвращается роутом одобрения. Одобрено значит обычный путь возврата целиком по
всему заказу, отклонено значит заказ остаётся отгруженным и заявка не создаётся

Частичный возврат по перечислению не поддерживается ни на одном шаге: решение
принимается по заказу целиком
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import Order, PendingEvent
from .outbox_service import OutboxIdentityConflict, queue_outbox_event


RETURN_APPROVAL_NOTIFICATION_KIND = "return_transfer_approval_request"
RETURN_APPROVAL_CALLBACK_PREFIX = "return_approval:"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"

RETURN_APPROVAL_STATUS_PENDING = "pending"
RETURN_APPROVAL_STATUS_APPROVED = "approved"
RETURN_APPROVAL_STATUS_REJECTED = "rejected"

RETURN_APPROVAL_DECISIONS = (RETURN_APPROVAL_STATUS_APPROVED, RETURN_APPROVAL_STATUS_REJECTED)


def _text(value) -> str:
    return str(value or "").strip()


def return_approval_state(order: Order) -> dict:
    state = (order.raw_payload or {}).get("return_approval")
    return dict(state) if isinstance(state, dict) else {}


def return_approval_status(order: Order) -> str:
    return _text(return_approval_state(order).get("status"))


def return_approval_is_pending(order: Order) -> bool:
    return return_approval_status(order) == RETURN_APPROVAL_STATUS_PENDING


def return_approval_idempotency_key(order_id: str, requested_at: str) -> str:
    return f"telegram:notification:return-approval:{order_id}:{requested_at}"


def return_approval_request_text(order: Order) -> str:
    raw_payload = order.raw_payload or {}
    items = list(order.items or [])
    blocks = sum(int(item.quantity_blocks or 0) for item in items)
    lines = [
        "Возврат по перечислению, нужно ваше решение",
        "",
        f"Заявка: {_text(raw_payload.get('skladbot_request_number')) or _text(order.external_id) or '-'}",
        f"Клиент: {_text(order.client) or '-'}",
        f"Отгружен: {order.order_date.strftime('%d.%m.%Y') if order.order_date else '-'}",
        f"Состав: {len(items)} SKU, {blocks} блок.",
        f"Тип оплаты: {_text(order.payment_type) or '-'}",
        "",
        "Возврат оформляется целиком по всему заказу",
    ]
    return "\n".join(lines)


def queue_return_approval_request(db: Session, order: Order, *, requested_by: str) -> PendingEvent | None:
    """Поставить запрос решения в очередь уведомлений владельца.

    Маршрут уведомления проверяет контракт: вид сообщения ведёт только в личный
    чат владельца, поэтому кнопки одобрения не могут уехать в клиентскую группу
    """
    order_id = str(order.id)
    requested_at = datetime.now(timezone.utc).isoformat()
    idempotency_key = return_approval_idempotency_key(order_id, requested_at)
    try:
        event = queue_outbox_event(
            db,
            event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
            action=RETURN_APPROVAL_NOTIFICATION_KIND,
            aggregate_type="order",
            aggregate_id=order_id,
            idempotency_key=idempotency_key,
            payload={
                "version": 1,
                "kind": RETURN_APPROVAL_NOTIFICATION_KIND,
                "text": return_approval_request_text(order),
                "return_approval_order_id": order_id,
                "requested_at": requested_at,
                "requested_by": _text(requested_by) or "desktop",
            },
        )
    except OutboxIdentityConflict:
        return None

    raw_payload = dict(order.raw_payload or {})
    raw_payload["return_approval"] = {
        "status": RETURN_APPROVAL_STATUS_PENDING,
        "requested_at": requested_at,
        "requested_by": _text(requested_by) or "desktop",
        "notification_event_id": str(event.id),
    }
    order.raw_payload = raw_payload
    return event


def mark_return_approval_decided(order: Order, *, decision: str, decided_by: str) -> dict:
    raw_payload = dict(order.raw_payload or {})
    state = dict(raw_payload.get("return_approval") or {})
    state["status"] = decision
    state["decided_at"] = datetime.now(timezone.utc).isoformat()
    state["decided_by"] = _text(decided_by) or "telegram"
    raw_payload["return_approval"] = state
    order.raw_payload = raw_payload
    return state
