"""Attach authenticated request identity to every audit row in its DB session."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.orm import Session

from .models import AuditLog


AUDIT_ACTOR_INFO_KEY = "taksklad_authenticated_audit_actor"


@dataclass(frozen=True)
class AuditActor:
    subject: str
    user_id: uuid.UUID | None = None
    service_principal_id: uuid.UUID | None = None


def bind_audit_actor(db, auth_context) -> AuditActor:
    user_id = parse_uuid(getattr(auth_context, "user_id", ""))
    principal_id = parse_uuid(getattr(auth_context, "principal_id", ""))
    source = str(getattr(auth_context, "source", "") or "authenticated")
    login = str(getattr(auth_context, "login", "") or "unknown")
    if user_id is not None:
        subject = f"user:{user_id}"
    elif principal_id is not None:
        subject = f"service:{login}"
    elif source == "web-session":
        subject = f"user:{login}"
    elif source == "legacy-service-token":
        subject = f"service:{login}"
    else:
        subject = f"{source}:{login}"
    actor = AuditActor(subject=subject[:120], user_id=user_id, service_principal_id=principal_id)
    return set_audit_actor(db, actor)


def set_audit_actor(db, actor: AuditActor) -> AuditActor:
    db.info[AUDIT_ACTOR_INFO_KEY] = actor
    return actor


def parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value or ""))
    except (ValueError, TypeError, AttributeError):
        return None


@event.listens_for(Session, "before_flush")
def attach_authenticated_audit_actor(session, _flush_context, _instances) -> None:
    actor = session.info.get(AUDIT_ACTOR_INFO_KEY)
    if not isinstance(actor, AuditActor):
        return
    for row in session.new:
        if not isinstance(row, AuditLog):
            continue
        row.actor_user_id = actor.user_id
        row.actor_service_principal_id = actor.service_principal_id
        row.actor_subject = actor.subject
        payload = dict(row.payload or {})
        claimed_actor = str(payload.get("actor") or "").strip()
        if claimed_actor and claimed_actor != actor.subject:
            payload["claimed_actor"] = claimed_actor[:120]
        payload["actor"] = actor.subject
        payload["authenticated_subject"] = actor.subject
        row.payload = payload
