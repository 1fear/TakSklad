"""Bounded, process-local operational metrics for the private diagnostics API."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock
from time import monotonic

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from .models import ImportJob, PendingEvent, WorkerHeartbeat
from .settings import APP_VERSION


REQUEST_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "OTHER"})
ROUTE_GROUPS = frozenset({
    "admin",
    "auth",
    "health",
    "imports",
    "metrics",
    "orders",
    "other",
    "readiness",
    "reports",
    "returns",
    "scans",
})
REQUEST_OUTCOMES = frozenset({"success", "client_error", "server_error"})
LATENCY_BUCKETS_SECONDS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
MAINTENANCE_MARKER = Path("/run/taksklad-observability/maintenance.json")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def route_group(path: str) -> str:
    normalized = str(path or "").strip("/")
    if normalized in {"health", "ready"}:
        return "health" if normalized == "health" else "readiness"
    segments = normalized.split("/")
    if segments[:2] != ["api", "v1"] or len(segments) < 3:
        return "other"
    candidate = segments[2]
    if candidate == "admin" and len(segments) > 3 and segments[3] == "metrics":
        return "metrics"
    return candidate if candidate in ROUTE_GROUPS else "other"


def request_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "success"


@dataclass(frozen=True)
class RequestMetric:
    method: str
    route_group: str
    outcome: str
    duration_seconds: float


class BoundedMetricsRegistry:
    """Small in-memory registry whose label domains are fixed in source."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._latency_buckets: Counter[tuple[str, str, float]] = Counter()
        self._latency_count: Counter[tuple[str, str]] = Counter()
        self._latency_sum: Counter[tuple[str, str]] = Counter()
        self._recent_request_durations: deque[float] = deque(maxlen=4096)
        self._maintenance_success: dict[str, datetime] = {}

    def observe_request(self, metric: RequestMetric) -> None:
        method = metric.method if metric.method in REQUEST_METHODS else "OTHER"
        group = metric.route_group if metric.route_group in ROUTE_GROUPS else "other"
        outcome = metric.outcome if metric.outcome in REQUEST_OUTCOMES else "server_error"
        duration = max(0.0, min(float(metric.duration_seconds), 300.0))
        with self._lock:
            self._requests[(method, group, outcome)] += 1
            self._latency_count[(method, group)] += 1
            self._latency_sum[(method, group)] += duration
            self._recent_request_durations.append(duration)
            for bucket in LATENCY_BUCKETS_SECONDS:
                if duration <= bucket:
                    self._latency_buckets[(method, group, bucket)] += 1

    def record_maintenance_success(self, kind: str, timestamp: datetime | None = None) -> None:
        if kind not in {"backup", "restore_drill"}:
            raise ValueError("unsupported maintenance signal")
        value = timestamp or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        with self._lock:
            self._maintenance_success[kind] = value.astimezone(timezone.utc)

    def render(
        self,
        *,
        db_pool: dict[str, int] | None = None,
        runtime: dict | None = None,
        maintenance: dict[str, datetime] | None = None,
    ) -> str:
        db_values = db_pool or {}
        runtime_values = runtime or {}
        lines = [
            "# HELP taksklad_requests_total Bounded API request count.",
            "# TYPE taksklad_requests_total counter",
        ]
        with self._lock:
            for labels, value in sorted(self._requests.items()):
                method, group, outcome = labels
                lines.append(
                    f'taksklad_requests_total{{method="{method}",route_group="{group}",outcome="{outcome}"}} {value}'
                )
            lines.extend([
                "# HELP taksklad_request_duration_seconds Bounded API request latency.",
                "# TYPE taksklad_request_duration_seconds histogram",
            ])
            for labels, count in sorted(self._latency_count.items()):
                method, group = labels
                for bucket in LATENCY_BUCKETS_SECONDS:
                    value = self._latency_buckets[(method, group, bucket)]
                    lines.append(
                        f'taksklad_request_duration_seconds_bucket{{method="{method}",route_group="{group}",le="{bucket:g}"}} {value}'
                    )
                lines.append(
                    f'taksklad_request_duration_seconds_bucket{{method="{method}",route_group="{group}",le="+Inf"}} {count}'
                )
                lines.append(
                    f'taksklad_request_duration_seconds_count{{method="{method}",route_group="{group}"}} {count}'
                )
                lines.append(
                    f'taksklad_request_duration_seconds_sum{{method="{method}",route_group="{group}"}} {self._latency_sum[labels]:.6f}'
                )
            maintenance_values = {**self._maintenance_success, **(maintenance or {})}
            total_requests = sum(self._requests.values())
            server_errors = sum(
                value for (_method, _group, outcome), value in self._requests.items()
                if outcome == "server_error"
            )
            recent_durations = sorted(self._recent_request_durations)
        now = datetime.now(timezone.utc)
        backup_at = maintenance_values.get("backup")
        drill_at = maintenance_values.get("restore_drill")
        gauges = {
            "taksklad_db_pool_checked_out": int(db_values.get("checked_out", 0)),
            "taksklad_db_pool_checked_in": int(db_values.get("checked_in", 0)),
            "taksklad_db_pool_size": int(db_values.get("size", 0)),
            "taksklad_queue_oldest_pending_age_seconds": float(runtime_values.get("queue_age", 0)),
            "taksklad_queue_pickup_seconds": float(runtime_values.get("queue_pickup", 0)),
            "taksklad_import_last_success_age_seconds": float(runtime_values.get("import_age", now.timestamp())),
            "taksklad_provider_failure_events": int(runtime_values.get("provider_failures", 0)),
            "taksklad_backup_last_success_age_seconds": _age_seconds(backup_at, now),
            "taksklad_restore_drill_last_success_age_seconds": _age_seconds(drill_at, now),
            "taksklad_readiness": 1 if runtime_values.get("readiness") is True else 0,
            "taksklad_http_5xx_ratio": server_errors / total_requests if total_requests else 0.0,
            "taksklad_http_p95_seconds": _quantile(recent_durations, 0.95),
        }
        for name, value in gauges.items():
            lines.extend((f"# TYPE {name} gauge", f"{name} {value}"))
        for worker_name, age_seconds in sorted((runtime_values.get("workers") or {}).items()):
            if worker_name not in WORKER_NAMES:
                continue
            lines.append(
                f'taksklad_worker_last_heartbeat_age_seconds{{worker="{worker_name}"}} {max(0, int(age_seconds))}'
            )
        identity = runtime_values.get("identity") or {}
        commit_sha = str(identity.get("commit_sha") or os.environ.get("TAKSKLAD_COMMIT_SHA") or "")
        image_digest = str(identity.get("image_digest") or os.environ.get("TAKSKLAD_IMAGE_DIGEST") or "")
        version = str(identity.get("version") or APP_VERSION)
        identity_valid = bool(
            SHA_RE.fullmatch(commit_sha)
            and DIGEST_RE.fullmatch(image_digest)
            and re.fullmatch(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", version)
        )
        # Exact values remain on authenticated /health verification. Metrics
        # expose only validity, avoiding unbounded build identifiers as labels.
        lines.extend((
            "# TYPE taksklad_runtime_identity_valid gauge",
            f"taksklad_runtime_identity_valid {1 if identity_valid else 0}",
        ))
        return "\n".join(lines) + "\n"


registry = BoundedMetricsRegistry()

WORKER_NAMES = frozenset({"google_sheets_sync", "skladbot", "smartup_auto_import", "telegram"})
PROVIDER_EVENT_PREFIXES = ("google_", "skladbot_", "smartup_", "telegram_")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_seconds(value: datetime | None, now: datetime) -> int:
    normalized = _aware(value)
    if normalized is None:
        return int(now.timestamp())
    return max(0, int((now - normalized).total_seconds()))


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, int((len(values) - 1) * quantile + 0.999999)))
    return float(values[index])


def runtime_signal_snapshot(db, *, now: datetime | None = None) -> dict:
    """Read bounded queue/import/provider/worker state without identifier labels."""
    timestamp = _aware(now) or datetime.now(timezone.utc)
    events = db.execute(
        select(PendingEvent).order_by(PendingEvent.created_at.desc()).limit(500)
    ).scalars().all()
    imports = db.execute(
        select(ImportJob).order_by(ImportJob.created_at.desc()).limit(100)
    ).scalars().all()
    workers = db.execute(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_name).limit(len(WORKER_NAMES))
    ).scalars().all()
    active_events = [event for event in events if event.status in {"pending", "processing", "blocked"}]
    oldest_active = min((_aware(event.created_at) for event in active_events), default=None)
    pickup_samples = []
    for event in events:
        created_at = _aware(event.created_at)
        updated_at = _aware(event.updated_at)
        # There is no immutable claimed_at column. While an event is processing,
        # updated_at is a bounded conservative proxy for its latest claim/progress
        # time. Completed rows are excluded because updated_at is completion time.
        if created_at is not None and updated_at is not None and event.status == "processing":
            pickup_samples.append(max(0.0, (updated_at - created_at).total_seconds()))
    successful_imports = [item for item in imports if item.status in {"completed", "completed_with_errors"}]
    latest_import = max((_aware(item.created_at) for item in successful_imports), default=None)
    provider_failures = sum(
        1
        for event in events
        if event.status in {"failed", "error", "dead"}
        and str(event.event_type or "").startswith(PROVIDER_EVENT_PREFIXES)
    )
    return {
        "queue_age": _age_seconds(oldest_active, timestamp) if oldest_active else 0,
        "queue_pickup": round(sum(pickup_samples) / len(pickup_samples), 6) if pickup_samples else 0,
        "import_age": _age_seconds(latest_import, timestamp),
        "provider_failures": provider_failures,
        "workers": {
            row.worker_name: _age_seconds(_aware(row.last_cycle_started_at), timestamp)
            for row in workers
            if row.worker_name in WORKER_NAMES
        },
    }


def read_maintenance_timestamps(path: Path = MAINTENANCE_MARKER) -> dict[str, datetime]:
    """Read only two approved timestamps from a small read-only collector marker."""
    try:
        if not path.is_file() or path.stat().st_size > 1024:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result = {}
    for kind, key in (("backup", "backup_success_at"), ("restore_drill", "restore_drill_success_at")):
        try:
            parsed = datetime.fromisoformat(str(payload.get(key) or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            result[kind] = parsed.astimezone(timezone.utc)
    return result


def db_pool_snapshot(db) -> dict[str, int]:
    pool = getattr(getattr(db, "bind", None), "pool", None)
    if pool is None:
        return {"checked_out": 0, "checked_in": 0, "size": 0}
    values = {}
    for output_name, method_name in (("checked_out", "checkedout"), ("checked_in", "checkedin"), ("size", "size")):
        method = getattr(pool, method_name, None)
        values[output_name] = max(0, int(method())) if callable(method) else 0
    return values


class OperationalMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            registry.observe_request(RequestMetric(
                method=str(request.method or "").upper(),
                route_group=route_group(request.url.path),
                outcome=request_outcome(status_code),
                duration_seconds=monotonic() - started,
            ))
