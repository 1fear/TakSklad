"""Canonical payment policy resolution with legacy-compatible shadow outputs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import logging
import re
from threading import Lock

from .observability_context import log_trace


LOGGER = logging.getLogger(__name__)
PAYMENT_POLICY_DOMAIN = frozenset({"terminal", "transfer", "unknown"})


class _PaymentPolicyShadowRegistry:
    """Process-local counter bounded by the fixed payment-policy domain."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: Counter[tuple[str, str, str]] = Counter()

    def observe(self, classification: tuple[str, str, str]) -> tuple[bool, int]:
        if any(value not in PAYMENT_POLICY_DOMAIN for value in classification):
            raise ValueError("unsupported payment policy classification")
        with self._lock:
            self._counts[classification] += 1
            observations = self._counts[classification]
            return observations == 1, observations

    def count(self, classification: tuple[str, str, str]) -> int:
        with self._lock:
            return self._counts[classification]


_SHADOW_OBSERVATIONS = _PaymentPolicyShadowRegistry()


@dataclass(frozen=True, slots=True)
class PaymentPolicyResolution:
    canonical: str
    legacy_reports: str
    legacy_skladbot: str
    divergent: bool


def resolve_payment_policy(value) -> PaymentPolicyResolution:
    """Resolve canonical and legacy payment groups without changing callers yet."""
    source_text = str(value or "").strip()
    reports_text = source_text.lower().replace("ё", "е")
    skladbot_text = re.sub(r"[^0-9a-zа-я]+", " ", reports_text)
    skladbot_text = re.sub(r"\s+", " ", skladbot_text).strip()

    legacy_reports = _reports_payment_type(reports_text)
    # Canonical intentionally preserves the reports rule; only SkladBot can diverge.
    canonical = legacy_reports
    legacy_skladbot = _legacy_skladbot_payment_type(skladbot_text)
    result = PaymentPolicyResolution(
        canonical=canonical,
        legacy_reports=legacy_reports,
        legacy_skladbot=legacy_skladbot,
        divergent=canonical != legacy_skladbot,
    )
    _record_shadow_observation(result)
    return result


def _reports_payment_type(text: str) -> str:
    if "терминал" in text or "terminal" in text:
        return "terminal"
    if "перечис" in text or "безнал" in text or "transfer" in text:
        return "transfer"
    return "unknown"


def _legacy_skladbot_payment_type(text: str) -> str:
    if "терминал" in text:
        return "terminal"
    if "перечис" in text or "безнал" in text:
        return "transfer"
    return "unknown"


def _record_shadow_observation(result: PaymentPolicyResolution) -> None:
    if result.canonical != "unknown" and not result.divergent:
        return
    classification = (result.canonical, result.legacy_reports, result.legacy_skladbot)
    should_log, observations = _SHADOW_OBSERVATIONS.observe(classification)
    if not should_log:
        return
    # Only fixed-domain classifications reach observability; the client value
    # itself is deliberately omitted.
    log_trace(
        LOGGER,
        "payment_policy_shadow",
        canonical=result.canonical,
        divergent=result.divergent,
        legacy_reports=result.legacy_reports,
        legacy_skladbot=result.legacy_skladbot,
        observations=observations,
    )
