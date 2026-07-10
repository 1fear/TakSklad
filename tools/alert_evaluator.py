#!/usr/bin/env python3
"""Deterministic alert state machine consuming emitted Prometheus metrics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from time import monotonic


SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>-?[0-9.eE+]+)$")


def parse_prometheus_snapshot(text: str) -> dict[str, list[float]]:
    metrics: dict[str, list[float]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.fullmatch(line)
        if match:
            metrics.setdefault(match.group("name"), []).append(float(match.group("value")))
    return metrics


def matches(rule: dict, values: list[float]) -> bool:
    if not values:
        return False
    threshold = float(rule["threshold"])
    if rule["operator"] == "gt":
        return max(values) > threshold
    if rule["operator"] == "lt":
        return min(values) < threshold
    raise ValueError(f"unsupported operator: {rule['operator']}")


@dataclass
class AlertState:
    state: str = "inactive"
    failing_since: float | None = None


class LocalJsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def emit(self, event: dict) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")


class AlertEvaluator:
    def __init__(self, rules: list[dict], sink: LocalJsonlSink) -> None:
        self.rules = rules
        self.sink = sink
        self.states = {rule["name"]: AlertState() for rule in rules}

    def evaluate(self, metrics_text: str, *, evaluated_monotonic: float) -> list[dict]:
        snapshot = parse_prometheus_snapshot(metrics_text)
        transitions = []
        for rule in self.rules:
            state = self.states[rule["name"]]
            active = matches(rule, snapshot.get(rule["metric"], []))
            transition = None
            if active:
                if state.failing_since is None:
                    state.failing_since = evaluated_monotonic
                    state.state = "pending"
                if (
                    state.state != "firing"
                    and evaluated_monotonic - state.failing_since >= float(rule["for_seconds"])
                ):
                    state.state = "firing"
                    transition = "firing"
            else:
                if state.state == "firing":
                    transition = "resolved"
                state.state = "inactive"
                state.failing_since = None
            if transition:
                event = {
                    "alert": rule["name"],
                    "metric": rule["metric"],
                    "state": transition,
                    "evaluated_monotonic_seconds": round(evaluated_monotonic, 6),
                    "observed_monotonic_seconds": round(monotonic(), 6),
                }
                self.sink.emit(event)
                transitions.append(event)
        return transitions
