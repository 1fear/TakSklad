"""Durable Telegram delivery for completed transfer-payment KIZ source files."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .db import SessionLocal
from .kiz_reports_service import build_kiz_source_file_report_xlsx
from .models import PendingEvent
from .telegram_clients import TelegramProcessorDelegate
from .telegram_output_contract import transfer_kiz_export_caption
from .telegram_routing_contract import TelegramMessageKind, load_telegram_routing_contract
from .transfer_kiz_service import (
    TELEGRAM_NOTIFICATION_EVENT_TYPE,
    TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
    TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE,
    process_transfer_kiz_completion_check,
    transfer_kiz_delivery_readiness,
)


TRANSFER_KIZ_MAX_PRE_SEND_ATTEMPTS = 3
TRANSFER_KIZ_STALE_AFTER = timedelta(minutes=10)


class TelegramTransferKizProcessor(TelegramProcessorDelegate):
    def _session_factory(self):
        return getattr(self, "session_factory", None) or SessionLocal

    def process_pending_transfer_kiz_completions(self):
        self.recover_stale_transfer_kiz_events()
        processed = 0
        while claimed := self._claim(TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE):
            event_id, owner = claimed
            try:
                with self._session_factory()() as db:
                    event = db.get(PendingEvent, event_id)
                    if event is not None and event.status == "processing" and event.lease_owner == owner:
                        process_transfer_kiz_completion_check(db, event)
                        event.lease_owner = None
                        event.lease_expires_at = None
                        db.commit()
            except Exception as exc:
                self._finish_check_failure(event_id, owner, exc)
            processed += 1
        return processed

    def process_pending_transfer_kiz_deliveries(self):
        self.recover_stale_transfer_kiz_events()
        processed = 0
        while claimed := self._claim(TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE):
            event_id, owner = claimed
            self._deliver(event_id, owner)
            processed += 1
        return processed

    def recover_stale_transfer_kiz_events(self):
        cutoff = datetime.now(timezone.utc) - TRANSFER_KIZ_STALE_AFTER
        with self._session_factory()() as db:
            events = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type.in_((
                    TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE,
                    TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
                )))
                .where(PendingEvent.status == "processing")
                .where(PendingEvent.updated_at < cutoff)
            ).scalars().all()
            for event in events:
                payload = dict(event.payload or {})
                if event.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE and payload.get("delivery_started"):
                    self._block_delivery(db, event, "transfer_kiz_delivery_stale_after_start")
                    continue
                event.status = "pending"
                event.lease_owner = None
                event.lease_expires_at = None
                event.last_error = "transfer_kiz_stale_processing_requeued"
            db.commit()
        return len(events)

    def _claim(self, event_type):
        owner = f"transfer-kiz:{uuid.uuid4()}"
        now = datetime.now(timezone.utc)
        lease_expires_at = now + TRANSFER_KIZ_STALE_AFTER
        with self._session_factory()() as db:
            eligible = (
                select(PendingEvent)
                .where(PendingEvent.event_type == event_type)
                .where(PendingEvent.status.in_(("pending", "failed")))
                .where(PendingEvent.available_at <= now)
                .order_by(PendingEvent.available_at, PendingEvent.created_at, PendingEvent.id)
                .limit(1)
            )
            if db.bind.dialect.name == "postgresql":
                event = db.execute(eligible.with_for_update(skip_locked=True)).scalar_one_or_none()
                if event is None:
                    return None
                event.status = "processing"
                event.attempts = int(event.attempts or 0) + 1
                event.lease_owner = owner
                event.lease_expires_at = lease_expires_at
                db.commit()
                return event.id, owner

            candidate_id = eligible.with_only_columns(PendingEvent.id).scalar_subquery()
            claimed_id = db.execute(
                update(PendingEvent)
                .where(PendingEvent.id == candidate_id)
                .where(PendingEvent.event_type == event_type)
                .where(PendingEvent.status.in_(("pending", "failed")))
                .where(PendingEvent.available_at <= now)
                .values(
                    status="processing",
                    attempts=PendingEvent.attempts + 1,
                    lease_owner=owner,
                    lease_expires_at=lease_expires_at,
                    completed_at=None,
                    updated_at=now,
                )
                .returning(PendingEvent.id)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            db.commit()
            if claimed_id is None:
                return None
            return claimed_id, owner

    def _finish_check_failure(self, event_id, owner, error):
        with self._session_factory()() as db:
            event = db.get(PendingEvent, event_id)
            if event is None or event.status != "processing" or event.lease_owner != owner:
                return
            event.status = "failed"
            event.last_error = _safe_error(error)
            event.lease_owner = None
            event.lease_expires_at = None
            db.commit()

    def _deliver(self, event_id, owner):
        try:
            with self._session_factory()() as db:
                event = db.get(PendingEvent, event_id)
                if event is None or event.status != "processing" or event.lease_owner != owner:
                    return
                payload = dict(event.payload or {})
                if payload.get("delivery_started"):
                    self._block_delivery(db, event, "transfer_kiz_delivery_started_without_result")
                    db.commit()
                    return
                source_key = _text(payload.get("source_key"))
                readiness = transfer_kiz_delivery_readiness(db, source_key)
                if readiness["blockers"]:
                    self._block_delivery(db, event, "transfer_kiz_delivery_validation_failed")
                    db.commit()
                    return
                target = self._configured_client_target()
                content, filename = build_kiz_source_file_report_xlsx(
                    db, readiness["source_file"], source_key=source_key,
                )
                event.payload = {**payload, "delivery_started": True}
                event.lease_owner = None
                event.lease_expires_at = None
                db.commit()
        except Exception as exc:
            self._delivery_failure(event_id, owner, exc)
            return

        try:
            self.send_document(target, content, filename, caption=transfer_kiz_export_caption(readiness["source_file"]))
        except Exception as exc:
            self._delivery_failure(event_id, owner, exc, after_started=True)
            return

        with self._session_factory()() as db:
            event = db.get(PendingEvent, event_id)
            if event is None:
                return
            event.status = "completed"
            event.last_error = ""
            event.completed_at = datetime.now(timezone.utc)
            event.payload = {**(event.payload or {}), "completed_at": datetime.now(timezone.utc).isoformat()}
            event.lease_owner = None
            event.lease_expires_at = None
            db.commit()

    def _delivery_failure(self, event_id, owner, error, *, after_started=False):
        with self._session_factory()() as db:
            event = db.get(PendingEvent, event_id)
            if event is None or event.status in {"completed", "blocked"}:
                return
            started = bool((event.payload or {}).get("delivery_started"))
            if after_started or started:
                self._block_delivery(db, event, "transfer_kiz_delivery_ambiguous")
            else:
                self._retry_or_block(db, event, _safe_error(error))
            db.commit()

    def _retry_or_block(self, db, event, reason):
        if int(event.attempts or 0) >= TRANSFER_KIZ_MAX_PRE_SEND_ATTEMPTS:
            self._block_delivery(db, event, reason)
            return
        event.status = "failed"
        event.last_error = reason
        event.lease_owner = None
        event.lease_expires_at = None

    def _block_delivery(self, db, event, reason):
        event.status = "blocked"
        event.last_error = reason
        event.completed_at = datetime.now(timezone.utc)
        event.lease_owner = None
        event.lease_expires_at = None
        event.payload = {**(event.payload or {}), "manual_recovery_required": True}
        self._queue_admin_alert(db, event)

    def _queue_admin_alert(self, db, event):
        source_key = _text((event.payload or {}).get("source_key"))
        digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
        key = f"telegram:notification:v1:transfer-kiz-delivery:{digest}"
        existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == key)).scalar_one_or_none()
        if existing is not None:
            return existing
        alert = PendingEvent(
            event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
            status="pending",
            idempotency_key=key,
            payload={
                "kind": "daily_reconciliation_alert",
                "text": "TakSklad: передача КИЗ требует ручной проверки. Автоповтор отключён.",
            },
        )
        db.add(alert)
        return alert

    def _configured_client_target(self):
        contract = load_telegram_routing_contract()
        route = contract.route_for(TelegramMessageKind.TRANSFER_KIZ_EXPORT)
        if route.destination != "client":
            raise RuntimeError("transfer_kiz_client_route_invalid")
        setting = contract.roles["client"]["setting"]
        target = _text(os.environ.get(setting))
        if not target.startswith("-") or not target[1:].isdigit():
            raise RuntimeError("transfer_kiz_client_route_not_configured")
        return target


def _safe_error(error) -> str:
    return str(error or "transfer_kiz_delivery_failed")[:500]


def _text(value) -> str:
    return str(value or "").strip()
