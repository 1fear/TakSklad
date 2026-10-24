"""Автоперепроверка создания заявки SkladBot после POST с неизвестным исходом

POST без ответа (таймаут, обрыв, 5xx) оставляет событие в blocked/ambiguous: заявка
на стороне SkladBot могла создаться, а могла и нет, и повтор вслепую рискует дублем
Маркер TakSklad в заявку не уходит, поэтому найти её можно только по содержимому

Здесь та же сверка, которой такие заказы разбирались руками 14.10.2026: список наших
заявок отгрузки против заявок, уже известных TakSklad; заявка, которую TakSklad не знает,
это сирота, и потерянная заявка обязана оказаться среди сирот своего окна

- ровно одна читаемая сирота совпала с заказом по всем проверкам и больше ни с кем:
  заявка привязывается без POST
- в окне нет ни одной сироты, кроме привязываемых к другим событиям этого прогона, и так
  две проверки подряд с разносом: событие возвращается в очередь на повторный POST, один
  раз на событие; любая необъяснённая сирота, даже чужая по содержимому, это ручной разбор,
  потому что несовпадение по содержимому не доказывает, что заявка не наша
- остальное (неполный или неупорядоченный список, нечитаемая деталь, похожая сирота,
  изменённый заказ, лимит повторов): ручной разбор, как и раньше

Режим задаёт SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE: off (по умолчанию), observe (только
вердикты в аудит), link (привязка без повторного POST), full (привязка и повтор)
"""

import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Order, OrderItem, PendingEvent
from .order_statuses import INACTIVE_ORDER_STATUSES
from .representative_contacts import find_representative_contact
from .skladbot_client import SkladBotClient, env_int, sanitize_skladbot_error
from .skladbot_contracts import (
    business_timezone,
    business_today,
    canonical_remote_request_id,
    canonical_skladbot_request_link,
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
    skladbot_create_event_ownership_conflict_reason,
    skladbot_create_requests_mode,
    stable_payload_hash,
)


SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV = "SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE"
SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT_ENV = "SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT"
SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT_ENV = "SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT"
RECHECK_MODES = ("off", "observe", "link", "full")

# 10.09.2026 каждая из десяти потерянных заявок появилась раньше нашей следующей
# успешной отправки, не позже чем через 7,7 минуты после начала зависшего POST
# (оценка сверху по соседям, не распределение задержки); решающая вторая проверка
# идёт примерно через 15,5 минуты после начала POST
FIRST_CHECK_DELAY = timedelta(minutes=5)
CONFIRM_DELAY = timedelta(minutes=10)
RECHECK_INTERVAL = timedelta(minutes=10)
LIST_FAILURE_PAUSE = timedelta(minutes=3)
ERROR_PAUSE = timedelta(minutes=30)
CANDIDATE_MAX_AGE = timedelta(days=2)
LINKED_LOOKBACK = timedelta(days=30)
MAX_AUTO_RETRIES = 1
CANDIDATE_LIMIT = 200
DETAIL_TIME_BUDGET_SECONDS = 60
# список заявок отдаёт только день создания, без времени и пояса: два дня запаса
# покрывают любой сдвиг пояса SkladBot относительно бизнес-пояса
COVERAGE_MARGIN_DAYS = 2
ORPHAN_WINDOW_DAYS = 1


def skladbot_ambiguous_auto_recheck_mode(environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    mode = normalize_text(environ.get(SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV)).casefold()
    if mode in RECHECK_MODES:
        return mode
    if mode:
        logging.warning("%s=%r is not one of %s, recheck is off", SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV, mode, RECHECK_MODES)
    return "off"


def skladbot_ambiguous_auto_recheck_enabled(environ: dict[str, str] | None = None) -> bool:
    return skladbot_ambiguous_auto_recheck_mode(environ) != "off"


def default_recheck_result(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "checked": 0,
        "clean_first": 0,
        "requeued": 0,
        "linked": 0,
        "manual": 0,
        "unproven": 0,
        "observed": 0,
        "errors": [],
    }


def recheck_ambiguous_skladbot_creates(
    db: Session,
    client: Any | None = None,
    *,
    now: datetime | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    mode = skladbot_ambiguous_auto_recheck_mode()
    if mode == "off":
        return default_recheck_result(status="disabled")
    if skladbot_create_requests_mode() != "enabled":
        return default_recheck_result(status="create_disabled")
    now = now or datetime.now(timezone.utc)

    # пока чужой POST в полёте или ждёт очереди, его будущая заявка выглядела бы сиротой
    if create_queue_busy(db):
        db.commit()
        return default_recheck_result(status="create_busy")
    candidates = select_recheck_candidates(db, now)
    if not candidates:
        db.commit()
        return default_recheck_result()
    # «самая новая известная заявка» снимается до списка: привязанная позже не делает список отставшим
    linked_before = load_linked_request_ids(db, now)
    # база закрывается до сети: транзакция не простаивает, пока SkladBot отвечает
    db.commit()

    result = default_recheck_result()
    client = client or SkladBotClient(progress_callback=progress_callback)
    if not getattr(client, "configured", False):
        return default_recheck_result(status="not_configured")
    # 14.10.2026 500 заявок доходили ровно до границы покрытия, запас глубины нулевой
    list_limit = max(100, min(env_int(SKLADBOT_AMBIGUOUS_RECHECK_LIST_LIMIT_ENV, 1000), 2000))
    try:
        list_items = [item for item in client.list_requests(limit=list_limit) if isinstance(item, dict)]
    except Exception as exc:
        error = sanitize_skladbot_error(exc)
        for candidate in candidates:
            pause_candidate(db, candidate, now + LIST_FAILURE_PAUSE, last_error=error)
        result["status"] = "list_failed"
        result["errors"].append(error)
        return result

    linked = linked_before | load_linked_request_ids(db, now)
    db.commit()
    listed = listed_requests_by_id(list_items)
    list_state = list_coverage(list_items, listed, linked_before)
    windows = {
        candidate["event_id"]: sorted(
            request_id
            for request_id, item in listed.items()
            if request_id not in linked and orphan_in_window(item, candidate["post_date"])
        )
        for candidate in candidates
    }
    orphans = read_orphan_details(client, listed, sorted({rid for window in windows.values() for rid in window}))
    evidence_base = {
        "mode": mode,
        "list_size": len(listed),
        "oldest_listed_date": list_state["oldest"].isoformat() if list_state["oldest"] else "",
        "list_days_back": (business_today(now) - list_state["oldest"]).days if list_state["oldest"] else None,
        "newest_listed_id": list_state["newest_id"],
        "newest_linked_id": list_state["newest_linked_id"],
        "list_sorted_newest_first": list_state["sorted"],
    }

    matches = {
        candidate["event_id"]: classify_orphans(db, candidate, windows[candidate["event_id"]], orphans)
        for candidate in candidates
    }
    db.commit()
    strong_owners: dict[int, int] = {}
    for verdict in matches.values():
        for request_id in verdict["strong"]:
            strong_owners[request_id] = strong_owners.get(request_id, 0) + 1
    explained = {
        request_id
        for verdict in matches.values()
        if len(verdict["strong"]) == 1 and strong_owners.get(verdict["strong"][0]) == 1
        for request_id in verdict["strong"]
    }

    for candidate in candidates:
        result["checked"] += 1
        window = windows[candidate["event_id"]]
        verdict = decide(
            candidate,
            matches[candidate["event_id"]],
            strong_owners,
            explained,
            list_state,
            window=window,
            orphans=orphans,
        )
        evidence = {**evidence_base, "orphan_ids": window, **verdict}
        try:
            apply_verdict(db, candidate, verdict, orphans, evidence, now, result, mode)
        except Exception as exc:
            db.rollback()
            error = sanitize_skladbot_error(exc)
            result["errors"].append({"event_id": str(candidate["event_id"]), "error": error})
            pause_candidate(db, candidate, now + ERROR_PAUSE, last_error=error)
    return result


def create_queue_busy(db: Session) -> bool:
    """POST создания заявки в полёте или с неизвестным исходом ещё не разобран

    Событие в очереди без начатого POST заявки не создаёт, пока его не возьмут в
    processing; событие удалённого заказа может висеть в failed бесконечно и не
    должно останавливать сверку (14.10.2026, повторная рецензия)
    """
    return bool(db.execute(
        select(func.count(PendingEvent.id))
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(or_(
            PendingEvent.status == "processing",
            (PendingEvent.status.in_(("pending", "failed")))
            & PendingEvent.payload["post_state"].as_string().in_(("started", "response_received")),
        ))
    ).scalar_one())


def select_recheck_candidates(db: Session, now: datetime) -> list[dict[str, Any]]:
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status == "blocked")
        .where(PendingEvent.payload["post_state"].as_string() == "ambiguous")
        .where(PendingEvent.created_at >= now - CANDIDATE_MAX_AGE)
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(CANDIDATE_LIMIT)
    ).scalars().all()
    today = business_today(now)
    candidates = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        # id из ответа POST значит, что заявка точно есть: это не сирота, а ручной разбор
        if canonical_remote_request_id(payload.get("post_response_request_id")):
            continue
        if skladbot_create_event_ownership_conflict_reason(event):
            continue
        ambiguous_at = parse_moment(payload.get("ambiguous_at")) or as_utc(event.updated_at)
        if ambiguous_at is None or now - ambiguous_at < FIRST_CHECK_DELAY:
            continue
        state = payload.get("auto_recheck") if isinstance(payload.get("auto_recheck"), dict) else {}
        next_check_at = parse_moment(state.get("next_check_at"))
        if next_check_at is not None and now < next_check_at:
            continue
        order_id = parse_uuid(payload.get("order_id"))
        order = db.get(Order, order_id) if order_id else None
        if order is None or not order_open_for_recheck(order, event, today):
            continue
        post_started_at = parse_moment(payload.get("post_started_at")) or ambiguous_at
        candidates.append({
            "event_id": event.id,
            "order_id": order.id,
            "ambiguous_at": normalize_text(payload.get("ambiguous_at")),
            "post_date": post_started_at.astimezone(business_timezone()).date(),
        })
    return candidates


def order_open_for_recheck(order: Order, event: PendingEvent, today) -> bool:
    """Заказ всё ещё ждёт именно эту заявку: активен, дата не прошла, связи нет, указатель на это событие."""
    raw_payload = order.raw_payload or {}
    if normalize_text(order.status) in INACTIVE_ORDER_STATUSES:
        return False
    if order.order_date is None or order.order_date < today:
        return False
    if normalize_text(raw_payload.get("skladbot_request_id")) or normalize_text(raw_payload.get("skladbot_request_number")):
        return False
    pointer = normalize_text(raw_payload.get("skladbot_create_event_id"))
    return not pointer or pointer == str(event.id)


def load_linked_request_ids(db: Session, now: datetime) -> set[int]:
    """Заявки, которые уже знает TakSklad: привязанные к заказам и полученные в ответ на POST

    Из raw_payload читаются отдельные ключи, а не весь документ: распаковка большого
    JSON по тысячам заказов стоит секунды
    """
    values = list(db.execute(
        select(Order.raw_payload["skladbot_request_id"].as_string())
        .where(Order.created_at >= now - LINKED_LOOKBACK)
    ).scalars())
    values.extend(
        request_id_from_number(number)
        for number in db.execute(
            select(Order.raw_payload["skladbot_request_number"].as_string())
            .where(Order.created_at >= now - LINKED_LOOKBACK)
        ).scalars()
    )
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


def request_id_from_number(number: Any) -> str:
    # id заявки SkladBot равен числовой части номера: WH-R-251621 это 251621
    match = re.search(r"(\d+)\s*$", normalize_text(number))
    return match.group(1) if match else ""


def listed_requests_by_id(list_items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    listed = {}
    for item in list_items:
        request_id = canonical_remote_request_id(request_list_value(item, "id"))
        if request_id:
            listed[int(request_id)] = item
    return listed


def list_coverage(
    list_items: list[dict[str, Any]],
    listed: dict[int, dict[str, Any]],
    linked_before: set[int],
) -> dict[str, Any]:
    dates = [parse_date(request_list_value(item, "created_at", "createdAt")) for item in list_items]
    known = [value for value in dates if value]
    return {
        "oldest": min(known) if known else None,
        # id в списке идут не строго по убыванию (снято 14.10.2026), а дни идут
        "sorted": len(known) == len(dates) and all(left >= right for left, right in zip(known, known[1:])),
        "newest_id": max(listed) if listed else 0,
        "newest_linked_id": max(linked_before) if linked_before else 0,
    }


def orphan_in_window(item: dict[str, Any], post_date) -> bool:
    created_on = parse_date(request_list_value(item, "created_at", "createdAt"))
    # без даты сирота остаётся в выборке: лишняя проверка безопаснее пропуска
    return created_on is None or created_on >= post_date - timedelta(days=ORPHAN_WINDOW_DAYS)


def read_orphan_details(
    client: Any,
    listed: dict[int, dict[str, Any]],
    orphan_ids: list[int],
) -> dict[int, dict[str, Any] | None]:
    """Деталь сироты, годная в доказательство, или None: тогда по окну ничего не утверждается."""
    detail_limit = max(1, env_int(SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT_ENV, 30))
    orphans: dict[int, dict[str, Any] | None] = {request_id: None for request_id in orphan_ids}
    if len(orphan_ids) > detail_limit:
        return orphans
    started = time.monotonic()
    for request_id in orphan_ids:
        if time.monotonic() - started >= DETAIL_TIME_BUDGET_SECONDS:
            break
        try:
            detail = client.get_request_detail(request_id)
        except Exception:
            continue
        orphans[request_id] = evidence_request(request_id, listed[request_id], detail)
    return orphans


def evidence_request(request_id: int, list_item: dict[str, Any], detail: Any) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    request = normalize_request_payload(list_item, detail)
    canonical_id, number = canonical_skladbot_request_link(detail.get("id"), request.get("number"))
    products = request.get("products") or []
    if canonical_id != str(request_id) or not number:
        return None
    if not request.get("recipient") or not request.get("unloading_date"):
        return None
    if not products or not all(product.get("name") and product.get("amount") for product in products):
        return None
    return request


def classify_orphans(
    db: Session,
    candidate: dict[str, Any],
    window: list[int],
    orphans: dict[int, dict[str, Any] | None],
) -> dict[str, list[int]]:
    order = load_order(db, candidate["order_id"])
    strong: list[int] = []
    similar: list[int] = []
    if order is None:
        return {"strong": strong, "similar": similar}
    for request_id in window:
        request = orphans.get(request_id)
        if request is None:
            continue
        diagnostics = request_match_diagnostics(order, request)
        checks = diagnostics.get("checks") or {}
        if diagnostics_strong(diagnostics):
            strong.append(request_id)
        elif checks.get("date") and checks.get("client"):
            similar.append(request_id)
    return {"strong": strong, "similar": similar}


def strong_match(order: Order, request: dict[str, Any]) -> bool:
    return diagnostics_strong(request_match_diagnostics(order, request))


def diagnostics_strong(diagnostics: dict[str, Any]) -> bool:
    return bool(diagnostics.get("matched")) and not diagnostics.get("extra_request_products")


def decide(
    candidate: dict[str, Any],
    matches: dict[str, list[int]],
    strong_owners: dict[int, int],
    explained: set[int],
    list_state: dict[str, Any],
    *,
    window: list[int],
    orphans: dict[int, dict[str, Any] | None],
) -> dict[str, Any]:
    if not list_state["sorted"]:
        return {"verdict": "unproven", "reason": "list_not_sorted_newest_first"}
    oldest = list_state["oldest"]
    if oldest is None or oldest > candidate["post_date"] - timedelta(days=COVERAGE_MARGIN_DAYS):
        return {"verdict": "unproven", "reason": "list_does_not_reach_before_post"}
    if list_state["newest_id"] < list_state["newest_linked_id"]:
        return {"verdict": "unproven", "reason": "list_misses_newest_linked_request"}
    if any(orphans.get(request_id) is None for request_id in window):
        return {"verdict": "unproven", "reason": "orphan_details_unavailable"}
    strong = matches["strong"]
    if len(strong) == 1 and strong_owners.get(strong[0]) == 1:
        # archived и is_completed не проверяются: SkladBot ставит их всем заявкам (14.10.2026, 500 из 500)
        return {"verdict": "link", "reason": "single_matching_orphan", "request_id": strong[0]}
    if strong:
        return {"verdict": "manual", "reason": "several_matching_orphans", "request_ids": strong}
    if matches["similar"]:
        return {"verdict": "manual", "reason": "similar_orphan", "request_ids": matches["similar"]}
    unexplained = [request_id for request_id in window if request_id not in explained]
    if unexplained:
        return {"verdict": "manual", "reason": "unexplained_orphan", "request_ids": unexplained}
    return {"verdict": "clean", "reason": "no_orphan_for_order"}


def apply_verdict(
    db: Session,
    candidate: dict[str, Any],
    verdict: dict[str, Any],
    orphans: dict[int, dict[str, Any] | None],
    evidence: dict[str, Any],
    now: datetime,
    result: dict[str, Any],
    mode: str,
) -> None:
    event = lock_event(db, candidate["event_id"])
    order = load_order(db, candidate["order_id"])
    if event is None or order is None or not still_ambiguous(event, candidate, order, now):
        db.commit()
        return
    payload = dict(event.payload or {})
    state = dict(payload.get("auto_recheck") or {})
    # чистая серия принадлежит одному POST: новый ambiguous_at начинает её заново
    if state.get("first_clean_ambiguous_at") != candidate["ambiguous_at"]:
        state.pop("first_clean_at", None)
        state.pop("first_clean_ambiguous_at", None)
    state["checks"] = int(state.get("checks") or 0) + 1
    state["last_checked_at"] = now.isoformat()
    state.pop("last_error", None)

    if mode == "observe":
        record_observation(db, event, payload, state, verdict, evidence, now, result)
        return

    if verdict["verdict"] == "link":
        # другой процесс мог узнать эту заявку, пока шла сверка: заказ или ответ POST соседа
        if verdict["request_id"] in load_linked_request_ids(db, now) or create_queue_busy(db):
            verdict = {"verdict": "manual", "reason": "orphan_linked_meanwhile", "request_id": verdict["request_id"]}
        elif not strong_match(order, orphans[verdict["request_id"]]):
            # совпадение проверялось до блокировки, заказ мог измениться с тех пор
            verdict = {"verdict": "manual", "reason": "order_changed_before_link", "request_id": verdict["request_id"]}

    if verdict["verdict"] == "link":
        request = orphans[verdict["request_id"]]
        request_payload = payload.get("request_payload") if isinstance(payload.get("request_payload"), dict) else {}
        state.pop("first_clean_at", None)
        state.update({"last_verdict": "linked", "last_reason": verdict["reason"]})
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
        if create_result.get("status") == "created_recovered":
            result["linked"] += 1
        else:
            result["manual"] += 1
        return

    if verdict["verdict"] == "clean":
        retries = int(state.get("retries") or 0)
        if mode != "full":
            verdict = {"verdict": "manual", "reason": "requeue_disabled_by_mode"}
        elif retries >= MAX_AUTO_RETRIES:
            verdict = {"verdict": "manual", "reason": "auto_retry_limit"}
        elif not order_payload_unchanged(db, order, payload):
            verdict = {"verdict": "manual", "reason": "order_changed_after_post"}
        elif not state.get("first_clean_at"):
            state.update({
                "first_clean_at": now.isoformat(),
                "first_clean_ambiguous_at": candidate["ambiguous_at"],
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

    record_verdict(db, event, payload, state, verdict, {**evidence, **verdict}, now)
    result[verdict["verdict"]] += 1


def record_verdict(
    db: Session,
    event: PendingEvent,
    payload: dict[str, Any],
    state: dict[str, Any],
    verdict: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    # чистая серия должна идти подряд: любой другой исход начинает её заново
    state.pop("first_clean_at", None)
    state.pop("first_clean_ambiguous_at", None)
    repeated = (state.get("last_verdict"), state.get("last_reason")) == (verdict["verdict"], verdict["reason"])
    state.update({
        "last_verdict": verdict["verdict"],
        "last_reason": verdict["reason"],
        "next_check_at": (now + RECHECK_INTERVAL).isoformat(),
    })
    event.payload = {**payload, "auto_recheck": state}
    if not repeated:
        add_recheck_audit(db, event, f"skladbot_create_auto_recheck_{verdict['verdict']}", evidence)
    db.commit()


def record_observation(
    db: Session,
    event: PendingEvent,
    payload: dict[str, Any],
    state: dict[str, Any],
    verdict: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime,
    result: dict[str, Any],
) -> None:
    repeated = (state.get("observed_verdict"), state.get("last_reason")) == (verdict["verdict"], verdict["reason"])
    # наблюдение не продолжает чистую серию: после возврата в full она начинается заново
    state.pop("first_clean_at", None)
    state.pop("first_clean_ambiguous_at", None)
    state.update({
        "observed_verdict": verdict["verdict"],
        "last_verdict": "observed",
        "last_reason": verdict["reason"],
        "next_check_at": (now + CONFIRM_DELAY).isoformat(),
    })
    event.payload = {**payload, "auto_recheck": state}
    if not repeated:
        add_recheck_audit(db, event, "skladbot_create_auto_recheck_observed", evidence)
    db.commit()
    result["observed"] += 1


def requeue_event(
    db: Session,
    event: PendingEvent,
    payload: dict[str, Any],
    state: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    for key in ("first_clean_at", "first_clean_ambiguous_at", "next_check_at"):
        state.pop(key, None)
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


def pause_candidate(db: Session, candidate: dict[str, Any], until: datetime, *, last_error: str = "") -> None:
    """Пауза после сбоя: следующий цикл не повторяет тот же тяжёлый запрос к SkladBot."""
    try:
        event = lock_event(db, candidate["event_id"])
        if event is None or event.status != "blocked":
            db.commit()
            return
        payload = dict(event.payload or {})
        state = dict(payload.get("auto_recheck") or {})
        state.pop("first_clean_at", None)
        state.pop("first_clean_ambiguous_at", None)
        state.update({"next_check_at": until.isoformat(), "last_error": last_error})
        event.payload = {**payload, "auto_recheck": state}
        db.commit()
    except Exception:
        db.rollback()


def order_payload_unchanged(db: Session, order: Order, payload: dict[str, Any]) -> bool:
    """Повторный POST уйдёт с текущим составом заказа: он обязан совпасть с тем, что ушло в первый раз

    Порядок позиций в теле не значим и между сборками меняется (14.10.2026 у трёх
    заказов из пяти), поэтому позиции сравниваются как набор
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


def still_ambiguous(event: PendingEvent, candidate: dict[str, Any], order: Order, now: datetime) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (
        event.status == "blocked"
        and normalize_text(payload.get("post_state")) == "ambiguous"
        and normalize_text(payload.get("ambiguous_at")) == candidate["ambiguous_at"]
        and not skladbot_create_event_ownership_conflict_reason(event)
        and order_open_for_recheck(order, event, business_today(now))
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
