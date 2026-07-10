#!/usr/bin/env python3
"""Feed synthetic emitted metrics through the real evaluator and local sink."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.observability_metrics import BoundedMetricsRegistry, RequestMetric
from tools.alert_evaluator import AlertEvaluator, LocalJsonlSink


RULES_PATH = ROOT / "monitoring/observability/alert-rules.json"


def emitted_snapshot(rule: dict, *, failure: bool) -> str:
    registry = BoundedMetricsRegistry()
    duration = 2.0 if rule["metric"] == "taksklad_http_p95_seconds" and failure else 0.1
    for _ in range(20):
        registry.observe_request(RequestMetric("GET", "orders", "success", duration))
    if rule["metric"] == "taksklad_http_5xx_ratio" and failure:
        for _ in range(10):
            registry.observe_request(RequestMetric("GET", "orders", "server_error", 0.1))
    now = datetime.now(timezone.utc)
    runtime = {
        "readiness": not (failure and rule["metric"] == "taksklad_readiness"),
        "queue_age": 600 if failure and rule["metric"] == "taksklad_queue_oldest_pending_age_seconds" else 0,
        "provider_failures": 2 if failure and rule["metric"] == "taksklad_provider_failure_events" else 0,
        "workers": {
            "skladbot": 240 if failure and rule["metric"] == "taksklad_worker_last_heartbeat_age_seconds" else 0,
        },
    }
    maintenance = {
        "backup": now - timedelta(seconds=172800 if failure and rule["metric"] == "taksklad_backup_last_success_age_seconds" else 60),
        "restore_drill": now - timedelta(seconds=1209600 if failure and rule["metric"] == "taksklad_restore_drill_last_success_age_seconds" else 60),
    }
    return registry.render(runtime=runtime, maintenance=maintenance)


def run_smoke(timeout_seconds: int) -> dict:
    config = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if config.get("destination") != "local-jsonl://temporary":
        raise ValueError("alert destination is not the approved local sink")
    rules = config.get("rules") or []
    if not rules:
        raise ValueError("alert rule catalog is empty")
    for rule in rules:
        if int(rule["for_seconds"]) > timeout_seconds or int(rule["for_seconds"]) > 300:
            raise ValueError(f"alert exceeds timeout: {rule['name']}")

    actual_started = monotonic()
    with tempfile.TemporaryDirectory(prefix="taksklad-alert-smoke-") as temporary:
        sink_path = Path(temporary) / "events.jsonl"
        evaluator = AlertEvaluator(rules, LocalJsonlSink(sink_path))
        synthetic_clock = actual_started
        raise_durations = []
        recovery_durations = []
        for rule in rules:
            failure = emitted_snapshot(rule, failure=True)
            evaluator.evaluate(failure, evaluated_monotonic=synthetic_clock)
            firing_clock = synthetic_clock + int(rule["for_seconds"])
            firing = evaluator.evaluate(failure, evaluated_monotonic=firing_clock)
            if [event["alert"] for event in firing] != [rule["name"]]:
                raise ValueError(f"synthetic failure did not fire: {rule['name']}")
            recovered_clock = firing_clock + 0.001
            resolved = evaluator.evaluate(
                emitted_snapshot(rule, failure=False), evaluated_monotonic=recovered_clock
            )
            if [event["alert"] for event in resolved] != [rule["name"]]:
                raise ValueError(f"synthetic recovery did not resolve: {rule['name']}")
            raise_durations.append(firing_clock - synthetic_clock)
            recovery_durations.append(recovered_clock - firing_clock)
            synthetic_clock = recovered_clock + 1.0
        delivered = [json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines()]
    actual_finished = monotonic()
    if len(delivered) != len(rules) * 2:
        raise ValueError("local sink did not receive every transition")
    observed_values = [event["observed_monotonic_seconds"] for event in delivered]
    if observed_values != sorted(observed_values):
        raise ValueError("observed transition timestamps are not monotonic")
    return {
        "destination": config["destination"],
        "alerts": len(rules),
        "firing": sum(event["state"] == "firing" for event in delivered),
        "resolved": sum(event["state"] == "resolved" for event in delivered),
        "maximum_raise_seconds": max(raise_durations, default=0),
        "maximum_recovery_seconds": max(recovery_durations, default=0),
        "observed_first_monotonic": observed_values[0],
        "observed_last_monotonic": observed_values[-1],
        "observed_elapsed_seconds": round(actual_finished - actual_started, 6),
        "external_sends": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.synthetic_only:
        parser.error("--synthetic-only is required; external delivery is unsupported")
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("timeout must be between 1 and 300 seconds")
    result = run_smoke(args.timeout_seconds)
    print("ALERT_SMOKE_OK " + " ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
