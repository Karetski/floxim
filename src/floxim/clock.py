"""Time, injected so tests of sleeps, timeouts and durations run instantly (spec §13)."""

from __future__ import annotations

import asyncio
import datetime
import time


class Clock:
    """Wall-clock time in UTC plus an asyncio sleep."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0))


class FakeClock(Clock):
    """A clock that only moves when told to, or when something sleeps on it."""

    def __init__(self, start: datetime.datetime | None = None) -> None:
        self._now = start or datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self._mono = 1000.0

    def now(self) -> datetime.datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += datetime.timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0))
        await asyncio.sleep(0)


def iso(moment: datetime.datetime) -> str:
    """ISO 8601 in UTC with milliseconds, as event timestamps use (spec §7.3)."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


def parse_iso(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
