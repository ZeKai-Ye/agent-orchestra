"""Injectable clock.

Lease TTL semantics must be testable, so nothing calls ``time.time()`` directly.
Subprocess-based tests inject a fixed instant through ``ORCHESTRA_NOW``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

EPOCH_ISO = "%Y-%m-%dT%H:%M:%SZ"


class Clock:
    """Wall clock in epoch seconds."""

    def now(self) -> float:
        return time.time()

    def iso(self, ts: float | None = None) -> str:
        value = self.now() if ts is None else ts
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime(EPOCH_ISO)


class FakeClock(Clock):
    """Deterministic clock for unit tests."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now


def clock_from_env(default: Clock | None = None) -> Clock:
    """Honour ``ORCHESTRA_NOW`` so CLI-level tests can freeze time."""

    raw = os.environ.get("ORCHESTRA_NOW", "").strip()
    if raw:
        try:
            return FakeClock(float(raw))
        except ValueError:
            pass
    return default or Clock()
