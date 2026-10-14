"""Автоперепроверка создания заявки SkladBot после POST с неизвестным исходом.

POST без ответа (таймаут, обрыв, 5xx) оставляет событие в blocked/ambiguous: заявка
на стороне SkladBot могла создаться, а могла и нет, и повтор вслепую рискует дублем.
Маркер TakSklad в заявку не уходит, поэтому найти её можно только по содержимому.

Здесь та же сверка, которой такие заказы разбирались руками 14.10.2026: список наших
заявок отгрузки против заявок, уже привязанных к заказам. Заявка, которую не знает ни
один заказ, это сирота, и потерянная заявка обязана оказаться среди сирот.

- ровно одна сирота совпала с заказом по всем проверкам: заявка привязывается без POST
- среди сирот нет ни одной того же клиента на ту же дату, и так две проверки подряд
  с разносом: событие возвращается в очередь на повторный POST, один раз на событие
- остальное (несколько совпавших, похожая сирота, нечитаемая сирота, список не доходит
  до заявок раньше POST, заказ изменился после POST): ручной разбор, как и раньше

Выключено по умолчанию флагом SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENABLED.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Order, OrderItem, PendingEvent
from .representative_contacts import find_representative_contact
from .skladbot_client import SkladBotClient, env_int, sanitize_skladbot_error
from .skladbot_contracts import (
    business_timezone,
    canonical_remote_request_id,
    normalize_request_payload,
    normalize_text,
    parse_date,
    request_list_value,
    request_match_diagnostics,
)
from .skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    build_order_dry_run,
    default_create_processing_result,
    finish_skladbot_create_event,
    save_skladbot_create_result,
    skladbot_create_requests_mode,
    stable_payload_hash,
)


SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV = "SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENABLED"
SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT_ENV = "SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT"
SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT_ENV = "SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT"

FIRST_CHECK_DELAY = timedelta(minutes=5)
CONFIRM_DELAY = timedelta(minutes=5)
RECHECK_INTERVAL = timedelta(minutes=10)
CANDIDATE_MAX_AGE = timedelta(days=2)
LINKED_LOOKBACK = timedelta(days=30)
MAX_AUTO_RETRIES = 1
# список заявок отдаёт только день создания, без времени и пояса: два дня запаса
# покрывают любой сдвиг пояса SkladBot относительно бизнес-пояса
COVERAGE_MARGIN_DAYS = 2
ORPHAN_WINDOW_DAYS = 1


def skladbot_ambiguous_auto_recheck_enabled(environ: dict[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return normalize_text(environ.get(SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV)).casefold() in {"1", "true", "yes", "on"}


def default_recheck_result(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "checked": 0,
        "clean_first": 0,
        "requeued": 0,
        "linked": 0,
        "manual": 0,
        "unproven": 0,
        "errors": [],
    }


def recheck_ambiguous_skladbot_creates(
    db: Session,
    client: Any | None = None,
    *,
    now: datetime | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not skladbot_ambiguous_auto_recheck_enabled():
        return default_recheck_result(status="disabled")
    if skladbot_create_requests_mode() != "enabled":
        return default_recheck_result(status="create_disabled")
    now = now or datetime.now(timezone.utc)

    candidates = select_recheck_candidates(db, now)
    # база закрывается до сети: транзакция не простаивает, пока SkladBot отвечает
    db.commit()
    result = default_recheck_result()
    if not candidates:
        return result

    client = client or SkladBotClient(progress_callback=progress_callback)
    if not getattr(client, "configured", False):
        return default_recheck_result(status="not_configured")
    list_limit = max(100, min(env_int(SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT_ENV, 500), 2000))
    try:
        list_items = [item for item in client.list_requests(limit=list_limit) if isinstance(item, dict)]
    except Exception as exc:
        result["status"] = "list_failed"
        result["errors"].append(sanitize_skladbot_error(exc))
        return result

    linked_ids = load_linked_request_ids(db, now)
    db.commit()
    listed = listed_requests_by_id(list_items)
    earliest_post_date = min(candidate["post_date"] for candidate in candidates)
    orphan_ids = sorted(
        request_id
        for request_id, item in listed.items()
        if request_id not in linked_ids and orphan_in_window(item, earliest_post_date)
    )
    detail_limit = max(1, env_int(SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT_ENV, 30))
    orphans: dict[int, dict[str, Any] | None] = {}
    if len(orphan_ids) <= detail_limit:
        for request_id in orphan_ids:
            try:
                detail = client.get_request_detail(request_id)
            except Exception:
                detail = None
            orphans[request_id] = (
                normalize_request_payload(listed[request_id], detail) if isinstance(detail, dict) else None
            )
    coverage = list_coverage(listed, linked_ids)
    evidence_base = {
        "list_size": len(listed),
        "oldest_listed_date": coverage["oldest"].isoformat() if coverage["oldest"] else "",
        "newest_listed_id": coverage["newest_id"],
        "newest_linked_id": coverage["newest_linked_id"],
        "orphan_ids": orphan_ids,
        "orphans_over_detail_limit": len(orphan_ids) > detail_limit,
    }

    matches = {
        candidate["event_id"]: classify_orphans(db, candidate, orphans)
        for candidate in candidates
    }
    strong_owners: dict[int, int] = {}
    for verdict in matches.values():
        for request_id in verdict["strong"]:
            strong_owners[request_id] = strong_owners.get(request_id, 0) + 1
    db.commit()

    for candidate in candidates:
        result["checked"] += 1
        verdict = decide(
            candidate,
            matches[candidate["event_id"]],
            strong_owners,
            coverage,
            orphans_complete=len(orphan_ids) <= detail_limit and all(
                orphans.get(request_id) is not None
                for request_id in orphan_ids
                if orphan_in_window(listed[request_id], candidate["post_date"])
            ),
        )
        apply_verdict(db, candidate, verdict, orphans, listed, evidence_base, now, result)
    return result


def select_recheck_candidates(db: Session, now: datetime) -> list[dict[str, Any]]:
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status == "blocked")
        .where(PendingEvent.payload["post_state"].as_string() == "ambiguous")
        .where(PendingEvent.created_at >= now - CANDIDATE_MAX_AGE)
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(50)
    ).scalars().all()
    candidates = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if normalize_text(payload.get("post_state")) != "ambiguous":
            continue
        # id из ответа POST значит, что заявка точно есть: это не сирота, а ручной разбор
        if canonical_remote_request_id(payload.get("post_response_request_id")):
            continue
        ambiguous_at = parse_moment(payload.get("ambiguous_at")) or as_utc(event.updated_at)
        if ambiguous_at is None or now - ambiguous_at < FIRST_CHECK_DELAY:
            continue
        state = payload.get("auto_recheck") if isinstance(payload.get("auto_recheck"), dict) else {}
        next_check_at = parse_moment(state.get("next_check_at"))
        if next_check_at is not None and now < next_check_at:
            continue
        post_started_at = parse_moment(payload.get("post_started_at")) or ambiguous_at
        order_id = parse_uuid(payload.get("order_id"))
        if order_id is None:
            continue
        candidates.append({
            "event_id": event.id,
            "order_id": order_id,
            "ambiguous_at": normalize_text(payload.get("ambiguous_at")),
            "post_date": post_started_at.astimezone(business_timezone()).date(),
        })
    return candidates


def load_linked_request_ids(db: Session, now: datetime) -> set[int]:
    """Заявки, которые уже знает TakSklad: привязанные к заказам и полученные в ответ на POST.

    Из raw_payload читается один ключ, а не весь документ: распаковка большого
    JSON по тысячам заказов стоит секунды.
    """
    values = list(db.execute(
        select(Order.raw_payload["skladbot_request_id"].as_string())
        .where(Order.created_at >= now - LINKED_LOOKBACK)
    ).scalars())
    for key in ("created_request_id", "post_response_request_id"):
        values.extend(db.execute(
            select(PendingEvent.payload[key].as_string())
            .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
            .where(PendingEvent.created_at >= now - CANDIDATE_MAX_AGE)
        ).scalars())
    linked: set[int] = set()
    for value in values:
        request_id = canonical_remote_request_id(value)
        if request_id:
            linked.add(int(request_id))
    return linked


def listed_requests_by_id(list_items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    listed = {}
    for item in list_items:
        request_id = canonical_remote_request_id(request_list_value(item, "id"))
        if request_id:
            listed[int(request_id)] = item
    return listed


def list_coverage(listed: dict[int, dict[str, Any]], linked_ids: set[int]) -> dict[str, Any]:
    dates = [
        parsed
        for parsed in (parse_date(request_list_value(item, "created_at", "createdAt")) for item in listed.values())
        if parsed
    ]
    return {
        "oldest": min(dates) if dates else None,
        "newest_id": max(listed) if listed else 0,
        "newest_linked_id": max(linked_ids) if linked_ids else 0,
    }


def orphan_in_window(item: dict[str, Any], post_date) -> bool:
    created_on = parse_date(request_list_value(item, "created_at", "createdAt"))
    # без даты сирота остаётся в выборке: лишняя проверка безопаснее пропуска
    return created_on is None or created_on >= post_date - timedelta(days=ORPHAN_WINDOW_DAYS)


def classify_orphans(
    db: Session,
    candidate: dict[str, Any],
    orphans: dict[int, dict[str, Any] | None],
) -> dict[str, list[int]]:
    order = load_order(db, candidate["order_id"])
    strong: list[int] = []
    similar: list[int] = []
    if order is None:
        return {"strong": strong, "similar": similar}
    for request_id, request in orphans.items():
        if request is None:
            continue
        diagnostics = request_match_diagnostics(order, request)
        checks = diagnostics.get("checks") or {}
        if diagnostics.get("matched") and not diagnostics.get("extra_request_products"):
            strong.append(request_id)
        elif checks.get("date") and checks.get("client"):
            similar.append(request_id)
    return {"strong": strong, "similar": similar}


def decide(
    candidate: dict[str, Any],
    matches: dict[str, list[int]],
    strong_owners: dict[int, int],
    coverage: dict[str, Any],
    *,
    orphans_complete: bool,
) -> dict[str, Any]:
    oldest = coverage["oldest"]
    if oldest is None or oldest > candidate["post_date"] - timedelta(days=COVERAGE_MARGIN_DAYS):
        return {"verdict": "unproven", "reason": "list_does_not_reach_before_post"}
    if coverage["newest_id"] < coverage["newest_linked_id"]:
        return {"verdict": "unproven", "reason": "list_misses_newest_linked_request"}
    if not orphans_complete:
        return {"verdict": "unproven", "reason": "orphan_details_unavailable"}
    strong = matches["strong"]
    if len(strong) == 1 and strong_owners.get(strong[0]) == 1:
        return {"verdict": "link", "reason": "single_matching_orphan", "request_id": strong[0]}
    if strong:
        return {"verdict": "manual", "reason": "several_matching_orphans", "request_ids": strong}
    if matches["similar"]:
        return {"verdict": "manual", "reason": "similar_orphan", "request_ids": matches["similar"]}
    return {"verdict": "clean", "reason": "no_orphan_for_order"}


def apply_verdict(
    db: Session,
    candidate: dict[str, Any],
    verdict: dict[str, Any],
    orphans: dict[int, dict[str, Any] | None],
    listed: dict[int, dict[str, Any]],
    evidence_base: dict[str, Any],
    now: datetime,
    result: dict[str, Any],
) -> None:
    event = lock_event(db, candidate["event_id"])
    order = load_order(db, candidate["order_id"])
    if event is None or order is None or not still_ambiguous(event, candidate, order):
        db.commit()
        return
    payload = dict(event.payload or {})
    state = dict(payload.get("auto_recheck") or {})
    state["checks"] = int(state.get("checks") or 0) + 1
    state["last_checked_at"] = now.isoformat()
    evidence = {**evidence_base, **verdict}

    if verdict["verdict"] == "link" and request_linked_to_order(db, verdict["request_id"]):
        # пока шла сверка, заявку привязал другой процесс: вторая привязка дала бы два заказа на одну заявку
        verdict = {"verdict": "manual", "reason": "orphan_linked_meanwhile", "request_id": verdict["request_id"]}

    if verdict["verdict"] == "link":
        request = orphans[verdict["request_id"]]
        request_payload = payload.get("request_payload") if isinstance(payload.get("request_payload"), dict) else {}
        state.update({"last_verdict": "linked", "last_reason": verdict["reason"]})
        state.pop("first_clean_at", None)
        event.payload = {**payload, "auto_recheck": state}
        add_recheck_audit(db, event, "skladbot_create_auto_recheck_linked", evidence)
        create_result = save_skladbot_create_result(
            db,
            order,
            event,
            request_payload,
            request,
            status="created_recovered",
        )
        finish_skladbot_create_event(db, event, create_result, default_create_processing_result())
        result["linked"] += 1
        return

    if verdict["verdict"] == "clean":
        retries = int(state.get("retries") or 0)
        if retries >= MAX_AUTO_RETRIES:
            verdict = {"verdict": "manual", "reason": "auto_retry_limit"}
        elif not order_payload_unchanged(db, order, payload):
            verdict = {"verdict": "manual", "reason": "order_changed_after_post"}
        elif not state.get("first_clean_at"):
            state.update({
                "first_clean_at": now.isoformat(),
                "next_check_at": (now + CONFIRM_DELAY).isoformat(),
                "last_verdict": "clean_first",
                "last_reason": verdict["reason"],
            })
            event.payload = {**payload, "auto_recheck": state}
            add_recheck_audit(db, event, "skladbot_create_auto_recheck_clean", evidence)
            db.commit()
            result["clean_first"] += 1
            return
        else:
            first_clean_at = parse_moment(state.get("first_clean_at"))
            if first_clean_at is None or now - first_clean_at < CONFIRM_DELAY:
                state["next_check_at"] = ((first_clean_at or now) + CONFIRM_DELAY).isoformat()
                event.payload = {**payload, "auto_recheck": state}
                db.commit()
                return
            requeue_event(db, event, payload, state, evidence, now)
            result["requeued"] += 1
            return

    # чистая серия должна идти подряд: любой другой исход начинает её заново
    state.pop("first_clean_at", None)
    repeated = (state.get("last_verdict"), state.get("last_reason")) == (verdict["verdict"], verdict["reason"])
    state.update({
        "last_verdict": verdict["verdict"],
        "last_reason": verdict["reason"],
        "next_check_at": (now + RECHECK_INTERVAL).isoformat(),
    })
    event.payload = {**payload, "auto_recheck": state}
    if not repeated:
        add_recheck_audit(db, event, f"skladbot_create_auto_recheck_{verdict['verdict']}", {**evidence, **verdict})
    db.commit()
    result[verdict["verdict"]] += 1


def requeue_event(
    db: Session,
    event: PendingEvent,
    payload: dict[str, Any],
    state: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    state.pop("first_clean_at", None)
    state.pop("next_check_at", None)
    state.update({
        "retries": int(state.get("retries") or 0) + 1,
        "requeued_at": now.isoformat(),
        "last_verdict": "requeued",
        "last_reason": evidence.get("reason") or "",
    })
    # тот же re-arm, которым такие события возвращались в очередь руками:
    # без retry_scheduled повторная попытка делает только reconcile и POST не шлёт
    event.payload = {
        **payload,
        "post_state": "retry_scheduled",
        "create_status": "queued",
        "auto_recheck": state,
    }
    event.status = "pending"
    event.attempts = 0
    # очередь сравнивает available_at с настоящими часами, событие доступно сразу
    event.available_at = datetime.now(timezone.utc)
    event.lease_owner = None
    event.lease_expires_at = None
    event.completed_at = None
    add_recheck_audit(db, event, "skladbot_create_auto_recheck_requeued", evidence)
    db.commit()


def order_payload_unchanged(db: Session, order: Order, payload: dict[str, Any]) -> bool:
    """Повторный POST уйдёт с текущим составом заказа: он обязан совпасть с тем, что ушло в первый раз.

    Порядок позиций в теле не значим и между сборками меняется (14.10.2026 у трёх
    заказов из пяти), поэтому позиции сравниваются как набор.
    """
    sent = payload.get("request_payload")
    if not isinstance(sent, dict) or not sent:
        return False
    dry_run = build_order_dry_run(
        order,
        list(order.items),
        str(payload.get("import_id") or ""),
        1,
        representative_contact=find_representative_contact(db, order.representative),
    )
    if dry_run.get("status") != "ready":
        return False
    return order_insensitive_hash(dry_run.get("payload") or {}) == order_insensitive_hash(sent)


def order_insensitive_hash(request_payload: dict[str, Any]) -> str:
    products = request_payload.get("products") if isinstance(request_payload.get("products"), list) else []
    return stable_payload_hash({
        **request_payload,
        "products": sorted(products, key=lambda product: stable_payload_hash({"product": product})),
    })


def request_linked_to_order(db: Session, request_id: int) -> bool:
    return db.execute(
        select(Order.id)
        .where(Order.raw_payload["skladbot_request_id"].as_string() == str(request_id))
        .limit(1)
    ).first() is not None


def still_ambiguous(event: PendingEvent, candidate: dict[str, Any], order: Order) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw_payload = order.raw_payload or {}
    return (
        event.status == "blocked"
        and normalize_text(payload.get("post_state")) == "ambiguous"
        and normalize_text(payload.get("ambiguous_at")) == candidate["ambiguous_at"]
        and not normalize_text(raw_payload.get("skladbot_request_id"))
        and not normalize_text(raw_payload.get("skladbot_request_number"))
    )


def lock_event(db: Session, event_id: uuid.UUID) -> PendingEvent | None:
    stmt = select(PendingEvent).where(PendingEvent.id == event_id).execution_options(populate_existing=True)
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return db.execute(stmt).scalars().one_or_none()


def load_order(db: Session, order_id: uuid.UUID) -> Order | None:
    return db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == order_id)
        .execution_options(populate_existing=True)
    ).scalars().unique().one_or_none()


def add_recheck_audit(db: Session, event: PendingEvent, action: str, evidence: dict[str, Any]) -> None:
    db.add(AuditLog(
        action=action,
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": normalize_text((event.payload or {}).get("order_id")),
            **evidence,
        },
    ))


def parse_moment(value: Any) -> datetime | None:
    text = normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
