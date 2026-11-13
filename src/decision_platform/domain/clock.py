"""Injectable clock so time-dependent behaviour (retry schedules, lease expiry,
document expiry rules) is deterministic in tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def iso(self) -> str:
        return self.now().isoformat(timespec="milliseconds")


class FixedClock(Clock):
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        self._now = when


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
