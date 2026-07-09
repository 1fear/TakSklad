import logging
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .backend_client import backend_configured, backend_read_orders_enabled, backend_request
from .backend_events import load_pending_backend_events
from .config import APP_DIR, APP_BUILD_LABEL, APP_VERSION, LOG_FILE, UPDATE_INFO_URL, UPDATE_LOG_FILE
from .http_client import open_https_url
from .orders import get_order_date_value, order_group_key
from .pending_store import load_pending_prints, load_pending_saves
from .startup_check import build_startup_self_check, build_version_update_status
from .telegram_service import load_pending_telegram
from .utils import normalize_text


def _count_list(value):
    return len(value) if isinstance(value, list) else 0


def _int_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_event_time(value):
    text = normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value, now=None):
    parsed = _parse_event_time(value)
    if parsed is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, int((now - parsed.astimezone(timezone.utc)).total_seconds()))


def format_queue_age(seconds):
    if seconds is None:
        return "-"
    if seconds < 60:
        return "меньше минуты"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def redact_diagnostic_text(value):
    text = normalize_text(value)
    if not text:
        return ""
    text = re.sub(r"Authorization\s*[:=]\s*Bearer\s+[^\s,;]+", "Authorization: [redacted]", text, flags=re.I)
    text = re.sub(r"Bearer\s+[^\s,;]+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"(?i)(token|password|private[_ -]?key|secret|chat_id)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"\b\d{7,}\b", "[redacted-number]", text)
    text = re.sub(r"\b[0-9A-Z]{18,}\b", "[redacted-code]", text)
    return text


def redact_diagnostic_value(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            safe_key = normalize_text(key)
            if safe_key.lower() in {"payload", "order", "codes", "products", "path", "caption", "last_error_detail"}:
                redacted[safe_key] = "[redacted]"
            else:
                redacted[safe_key] = redact_diagnostic_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    return value


def _event_error_class(item):
    text = normalize_text(item.get("last_error")).lower()
    detail = normalize_text(item.get("last_error_detail")).lower()
    combined = f"{text} {detail}"
    if "409" in combined or "conflict" in combined:
        return "conflict"
    if "429" in combined or "quota" in combined:
        return "quota"
    if "timeout" in combined or "504" in combined or "503" in combined:
        return "temporary"
    if "auth" in combined or "401" in combined or "403" in combined:
        return "auth"
    if combined.strip():
        return "error"
    return ""


def _queue_metrics(items, *, title, blocked=0, now=None):
    raw_items = list(items or [])
    items = [item for item in raw_items if isinstance(item, dict)]
    oldest_age = None
    failed = 0
    max_attempts = 0
    error_classes = set()
    for item in items:
        failed += 1 if normalize_text(item.get("last_error")) else 0
        max_attempts = max(max_attempts, _int_value(item.get("attempts")))
        error_class = _event_error_class(item)
        if error_class:
            error_classes.add(error_class)
        age = _age_seconds(item.get("created_at") or item.get("updated_at"), now=now)
        if age is not None:
            oldest_age = age if oldest_age is None else max(oldest_age, age)

    state = "ok"
    if blocked:
        state = "blocked"
    elif items:
        state = "pending"
    return {
        "title": title,
        "count": len(raw_items),
        "failed": failed,
        "blocked": blocked,
        "max_attempts": max_attempts,
        "oldest_age_seconds": oldest_age,
        "oldest_age": format_queue_age(oldest_age),
        "state": state,
        "error_classes": sorted(error_classes),
    }


def _backend_result_from_sync(sync_result):
    if not isinstance(sync_result, dict):
        return {}
    if isinstance(sync_result.get("backend"), dict):
        return sync_result["backend"]
    return sync_result


def _blocked_backend_events(sync_result, pending_events):
    backend_result = _backend_result_from_sync(sync_result)
    blocked_events = backend_result.get("blocked_events") if isinstance(backend_result, dict) else []
    result = [item for item in (blocked_events or []) if isinstance(item, dict)]
    for item in pending_events or []:
        if not isinstance(item, dict):
            continue
        if _event_error_class(item) == "conflict":
            result.append(item)
    return result


def build_sync_queue_summary(sync_result=None, now=None, google_available=True, backend_available=True):
    pending_saves = load_pending_saves()
    pending_prints = load_pending_prints()
    pending_telegram = load_pending_telegram()
    pending_backend_events = load_pending_backend_events()
    blocked_backend_events = _blocked_backend_events(sync_result, pending_backend_events)
    blocked_scan_count = sum(1 for item in blocked_backend_events if item.get("type") == "scan")
    blocked_complete_count = sum(1 for item in blocked_backend_events if item.get("type") == "order_complete")
    backend_scans = [item for item in pending_backend_events if isinstance(item, dict) and item.get("type") == "scan"]
    backend_completes = [
        item for item in pending_backend_events
        if isinstance(item, dict) and item.get("type") == "order_complete"
    ]
    backend_other = [
        item for item in pending_backend_events
        if not isinstance(item, dict) or item.get("type") not in {"scan", "order_complete"}
    ]

    queues = {
        "google_saves": _queue_metrics(pending_saves, title="Google записи", now=now),
        "backend_scans": _queue_metrics(
            backend_scans,
            title="Backend сканы",
            blocked=blocked_scan_count,
            now=now,
        ),
        "backend_completes": _queue_metrics(
            backend_completes,
            title="Backend завершения",
            blocked=blocked_complete_count,
            now=now,
        ),
        "backend_other": _queue_metrics(backend_other, title="Backend прочее", now=now),
        "prints": _queue_metrics(pending_prints, title="Печать", now=now),
        "telegram": _queue_metrics(pending_telegram, title="Telegram", now=now),
    }
    total_pending = sum(queue["count"] for queue in queues.values())
    total_blocked = sum(queue["blocked"] for queue in queues.values())
    retry_enabled = total_pending > 0 and not total_blocked
    retry_blocker = ""
    if total_blocked:
        retry_enabled = False
        retry_blocker = "Есть конфликт backend 409, нужна ручная проверка"
    elif queues["backend_scans"]["count"] + queues["backend_completes"]["count"] and not backend_available:
        retry_enabled = False
        retry_blocker = "Backend не настроен или недоступен"
    elif queues["google_saves"]["count"] and not google_available:
        retry_enabled = False
        retry_blocker = "Google Sheet ещё не загружен"
    elif total_pending == 0:
        retry_blocker = "Очередей нет"

    return {
        "queues": queues,
        "total_pending": total_pending,
        "total_blocked": total_blocked,
        "retry_enabled": retry_enabled,
        "retry_blocker": retry_blocker,
    }


def format_sync_queue_summary(summary):
    summary = summary if isinstance(summary, dict) else {}
    queues = summary.get("queues") if isinstance(summary.get("queues"), dict) else {}
    lines = [
        f"Всего в очередях: {int(summary.get('total_pending') or 0)}",
        f"Требуют проверки: {int(summary.get('total_blocked') or 0)}",
    ]
    for key in ("google_saves", "backend_scans", "backend_completes", "backend_other", "prints", "telegram"):
        queue = queues.get(key) if isinstance(queues.get(key), dict) else {}
        title = queue.get("title") or key
        parts = [
            f"{title}: {int(queue.get('count') or 0)}",
            f"статус={queue.get('state') or 'ok'}",
            f"возраст={queue.get('oldest_age') or '-'}",
            f"попытки={int(queue.get('max_attempts') or 0)}",
        ]
        if queue.get("failed"):
            parts.append(f"ошибок={int(queue.get('failed') or 0)}")
        if queue.get("blocked"):
            parts.append(f"требуют проверки={int(queue.get('blocked') or 0)}")
        if queue.get("error_classes"):
            parts.append("классы=" + ",".join(queue.get("error_classes") or []))
        lines.append(" · ".join(parts))
    if not summary.get("retry_enabled"):
        blocker = normalize_text(summary.get("retry_blocker"))
        if blocker:
            lines.append(f"Повтор: недоступен · {blocker}")
    else:
        lines.append("Повтор: доступен")
    return "\n".join(lines)


def classify_probe_exception(exc):
    message = normalize_text(exc).lower()
    if isinstance(exc, socket.gaierror) or any(marker in message for marker in ("getaddrinfo", "name or service", "nodename", "dns")):
        return "dns"
    if isinstance(exc, ssl.SSLError) or any(marker in message for marker in ("ssl", "certificate", "tls")):
        return "tls"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {401, 403}:
            return "auth_rejected"
        if exc.code in {408, 429, 500, 502, 503, 504}:
            return "backend_unavailable"
    if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden", "auth rejected")):
        return "auth_rejected"
    if any(marker in message for marker in ("timed out", "timeout", "connection refused", "unavailable", "502", "503", "504")):
        return "backend_unavailable"
    return "unknown"


def _probe_result(name, status, failure_class="", target=""):
    return {
        "name": name,
        "status": status,
        "class": failure_class,
        "target": target,
    }


def _url_host(url):
    parsed = urllib.parse.urlparse(normalize_text(url))
    return parsed.netloc


def probe_dns(hostname):
    hostname = normalize_text(hostname)
    if not hostname:
        return _probe_result("dns", "skipped", "not_configured")
    try:
        socket.getaddrinfo(hostname, 443)
        return _probe_result("dns", "ok", target=hostname)
    except Exception as exc:
        return _probe_result("dns", "failed", classify_probe_exception(exc), target=hostname)


def probe_https_manifest(url=UPDATE_INFO_URL):
    url = normalize_text(url)
    if not url:
        return _probe_result("github_manifest", "skipped", "not_configured")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "TakSklad-diagnostics"})
    try:
        with open_https_url(request, timeout=8) as response:
            response.read(256)
        return _probe_result("github_manifest", "ok", target=_url_host(url))
    except Exception as exc:
        return _probe_result("github_manifest", "failed", classify_probe_exception(exc), target=_url_host(url))


def run_readonly_diagnostic_probes():
    probes = []
    update_host = _url_host(UPDATE_INFO_URL)
    if update_host:
        probes.append(probe_dns(update_host))
    probes.append(probe_https_manifest())

    if backend_configured():
        try:
            backend_request("GET", "/health")
            probes.append(_probe_result("backend_health", "ok", target="backend"))
        except Exception as exc:
            probes.append(_probe_result("backend_health", "failed", classify_probe_exception(exc), target="backend"))
        if backend_read_orders_enabled():
            try:
                backend_request("GET", "/api/v1/orders/active")
                probes.append(_probe_result("backend_orders", "ok", target="backend"))
            except Exception as exc:
                probes.append(_probe_result("backend_orders", "failed", classify_probe_exception(exc), target="backend"))
    else:
        probes.append(_probe_result("backend_health", "skipped", "not_configured", target="backend"))

    try:
        check = build_startup_self_check()
        status = "ok" if check.get("credentials") in {"stored", "file"} else "failed"
        failure_class = "" if status == "ok" else "not_configured"
        probes.append(_probe_result("google_credentials", status, failure_class, target="google"))
    except Exception as exc:
        probes.append(_probe_result("google_credentials", "failed", classify_probe_exception(exc), target="google"))
    return probes


def classify_log_line(line):
    text = normalize_text(line).lower()
    if any(marker in text for marker in ("traceback", "exception", "error", "ошибка")):
        return "error"
    if any(marker in text for marker in ("warning", "предупреж", "timeout", "quota", "429")):
        return "warning"
    if "backend" in text:
        return "backend"
    if "google" in text:
        return "google"
    if "telegram" in text:
        return "telegram"
    if "update" in text or "обнов" in text:
        return "update"
    return "info"


def classify_log_tail(path, line_limit=80):
    result = {
        "exists": bool(path and os.path.exists(path)),
        "line_count": 0,
        "classes": {},
    }
    if not result["exists"]:
        return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file_obj:
            lines = file_obj.readlines()[-line_limit:]
    except Exception:
        result["classes"] = {"read_error": 1}
        return result
    result["line_count"] = len(lines)
    classes = {}
    for line in lines:
        line_class = classify_log_line(line)
        classes[line_class] = classes.get(line_class, 0) + 1
    result["classes"] = classes
    return result


def build_diagnostic_startup_self_check(version_status=None):
    check = dict(build_startup_self_check(version_status))
    for key in ("app_dir", "log_file"):
        if check.get(key):
            check[key] = "[redacted-path]"
    return check


def build_diagnostic_bundle_manifest(sync_result=None, probes=None):
    version_status = build_version_update_status()
    startup_check = build_diagnostic_startup_self_check(version_status)
    queue_summary = build_sync_queue_summary(sync_result=sync_result)
    manifest = {
        "bundle_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app": {
            "version": APP_VERSION,
            "build_label": APP_BUILD_LABEL,
            "workstation_id": startup_check.get("workstation_id"),
        },
        "startup_self_check": startup_check,
        "version_update_status": version_status,
        "queue_summary": queue_summary,
        "queue_summary_text": format_sync_queue_summary(queue_summary),
        "probes": probes if probes is not None else run_readonly_diagnostic_probes(),
        "log_tail_classes": {
            "app_log": classify_log_tail(LOG_FILE),
            "update_log": classify_log_tail(UPDATE_LOG_FILE),
        },
    }
    return redact_diagnostic_value(manifest)


def write_diagnostic_bundle(output_dir=None, sync_result=None, probes=None):
    output_dir = output_dir or os.path.join(APP_DIR, "diagnostics")
    os.makedirs(output_dir, exist_ok=True)
    manifest = build_diagnostic_bundle_manifest(sync_result=sync_result, probes=probes)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"TakSklad_diagnostics_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as file_obj:
        json.dump(manifest, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
    return path, manifest


def backend_event_diagnostic_counts(events):
    events = events if isinstance(events, list) else []
    result = {
        "pending_backend_scan_events": 0,
        "pending_backend_order_complete_events": 0,
        "pending_backend_other_events": 0,
        "pending_backend_failed_events": 0,
        "pending_backend_attempted_events": 0,
        "pending_backend_max_attempts": 0,
    }
    for item in events:
        if not isinstance(item, dict):
            result["pending_backend_other_events"] += 1
            continue
        event_type = normalize_text(item.get("type"))
        if event_type == "scan":
            result["pending_backend_scan_events"] += 1
        elif event_type == "order_complete":
            result["pending_backend_order_complete_events"] += 1
        else:
            result["pending_backend_other_events"] += 1
        if normalize_text(item.get("last_error")):
            result["pending_backend_failed_events"] += 1
        attempts = _int_value(item.get("attempts"))
        if attempts > 0:
            result["pending_backend_attempted_events"] += 1
        result["pending_backend_max_attempts"] = max(
            result["pending_backend_max_attempts"],
            attempts,
        )
    return result


def build_refresh_diagnostic_summary(orders, all_existing_codes, sync_result=None, source="google"):
    orders = orders if isinstance(orders, list) else []
    sync_result = sync_result if isinstance(sync_result, dict) else {}
    skladbot_result = sync_result.get("skladbot") if isinstance(sync_result.get("skladbot"), dict) else {}
    backend_result = sync_result.get("backend") if isinstance(sync_result.get("backend"), dict) else {}
    google_pending_result = (
        sync_result.get("google_sheets_pending")
        if isinstance(sync_result.get("google_sheets_pending"), dict)
        else {}
    )
    pending_backend_events = load_pending_backend_events()
    groups = {order_group_key(order) for order in orders if isinstance(order, dict)}
    order_dates = {
        normalize_text(get_order_date_value(order))
        for order in orders
        if isinstance(order, dict) and normalize_text(get_order_date_value(order))
    }

    primary_source = normalize_text(sync_result.get("primary_source")) or normalize_text(source) or "google"

    return {
        "source": primary_source,
        "primary_source": primary_source,
        "backend_only_refresh": bool(sync_result.get("backend_only_refresh")),
        "emergency_google_fallback": bool(sync_result.get("emergency_google_fallback")),
        "orders": len(orders),
        "groups": len(groups),
        "order_dates": len(order_dates),
        "known_codes": len(all_existing_codes or []),
        "pending_saves": _count_list(load_pending_saves()),
        "pending_prints": _count_list(load_pending_prints()),
        "pending_backend_events": _count_list(pending_backend_events),
        **backend_event_diagnostic_counts(pending_backend_events),
        "pending_telegram": _count_list(load_pending_telegram()),
        "sync_synced": int(sync_result.get("synced") or 0),
        "sync_failed": int(sync_result.get("failed") or 0),
        "sync_remaining": int(sync_result.get("remaining") or 0),
        "backend_enabled": bool(backend_result.get("enabled")),
        "backend_synced": int(backend_result.get("synced") or 0),
        "backend_failed": int(backend_result.get("failed") or 0),
        "backend_remaining": int(backend_result.get("remaining") or 0),
        "google_mirror_status": normalize_text(google_pending_result.get("status")) or "unknown",
        "google_mirror_synced_exports": int(google_pending_result.get("synced") or 0),
        "google_mirror_failed_exports": int(google_pending_result.get("failed") or 0),
        "google_mirror_pending_exports": int(google_pending_result.get("remaining") or 0),
        "skladbot_enabled": bool(skladbot_result.get("enabled")),
        "skladbot_matched": int(skladbot_result.get("matched") or 0),
        "skladbot_not_found": int(skladbot_result.get("not_found") or 0),
        "skladbot_multiple": int(skladbot_result.get("multiple") or 0),
        "skladbot_errors": int(skladbot_result.get("errors") or 0),
    }


def format_refresh_diagnostic_summary(summary):
    ordered_keys = [
        "source",
        "primary_source",
        "backend_only_refresh",
        "emergency_google_fallback",
        "orders",
        "groups",
        "order_dates",
        "known_codes",
        "pending_saves",
        "pending_prints",
        "pending_backend_events",
        "pending_backend_scan_events",
        "pending_backend_order_complete_events",
        "pending_backend_other_events",
        "pending_backend_failed_events",
        "pending_backend_attempted_events",
        "pending_backend_max_attempts",
        "pending_telegram",
        "sync_synced",
        "sync_failed",
        "sync_remaining",
        "backend_enabled",
        "backend_synced",
        "backend_failed",
        "backend_remaining",
        "google_mirror_status",
        "google_mirror_synced_exports",
        "google_mirror_failed_exports",
        "google_mirror_pending_exports",
        "skladbot_enabled",
        "skladbot_matched",
        "skladbot_not_found",
        "skladbot_multiple",
        "skladbot_errors",
    ]
    return "Refresh diagnostic summary: " + " ".join(
        f"{key}={summary.get(key)}" for key in ordered_keys
    )


def log_refresh_diagnostic_summary(orders, all_existing_codes, sync_result=None, source="google"):
    try:
        logging.info(
            format_refresh_diagnostic_summary(
                build_refresh_diagnostic_summary(
                    orders,
                    all_existing_codes,
                    sync_result=sync_result,
                    source=source,
                )
            )
        )
    except Exception:
        logging.exception("Refresh diagnostic summary failed")
