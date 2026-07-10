"""Thread-safe bounded login attempt limiter."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable


class LoginRateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = max(0.0, float(retry_after))
        super().__init__("too many login attempts")


class LoginLimiterCapacityExceeded(RuntimeError):
    def __init__(self):
        super().__init__("login limiter capacity exceeded")


@dataclass
class _AttemptRecord:
    window_start: float
    count: int
    locked_until: float
    expires_at: float


class BoundedTTLLoginLimiter:
    def __init__(
        self,
        *,
        max_entries: int,
        entry_ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if int(max_entries) < 1:
            raise ValueError("max_entries must be positive")
        if float(entry_ttl_seconds) <= 0:
            raise ValueError("entry_ttl_seconds must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._max_entries = int(max_entries)
        self._entry_ttl_seconds = float(entry_ttl_seconds)
        self._clock = clock
        self._records: dict[str, _AttemptRecord] = {}
        self._lock = Lock()

    def ensure_not_locked(self, key: str) -> None:
        with self._lock:
            now = float(self._clock())
            self._prune_expired(now)
            record = self._records.get(key)
            if record is not None and record.locked_until > now:
                raise LoginRateLimited(record.locked_until - now)

    def register_failure(
        self,
        key: str,
        *,
        max_attempts: int,
        window_seconds: float,
        lock_seconds: float,
    ) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key must not be empty")
        if int(max_attempts) < 1:
            raise ValueError("max_attempts must be positive")
        if float(window_seconds) <= 0:
            raise ValueError("window_seconds must be positive")
        if float(lock_seconds) <= 0:
            raise ValueError("lock_seconds must be positive")

        with self._lock:
            now = float(self._clock())
            self._prune_expired(now)
            record = self._records.get(key)
            if record is not None and record.locked_until > now:
                raise LoginRateLimited(record.locked_until - now)
            if record is None:
                if len(self._records) >= self._max_entries:
                    raise LoginLimiterCapacityExceeded()
                record = _AttemptRecord(now, 0, 0.0, now + self._entry_ttl_seconds)
                self._records[key] = record
            elif now - record.window_start >= float(window_seconds):
                record.window_start = now
                record.count = 0
                record.locked_until = 0.0

            record.count += 1
            if record.count >= int(max_attempts):
                record.locked_until = now + float(lock_seconds)
            record.expires_at = max(
                now + self._entry_ttl_seconds,
                record.window_start + float(window_seconds),
                record.locked_until,
            )

    def clear(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)

    def size(self) -> int:
        with self._lock:
            now = float(self._clock())
            self._prune_expired(now)
            return len(self._records)

    def _prune_expired(self, now: float) -> None:
        expired = [
            key
            for key, record in self._records.items()
            if record.expires_at <= now and record.locked_until <= now
        ]
        for key in expired:
            self._records.pop(key, None)
