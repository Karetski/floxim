"""Test support: an asyncio event loop with virtual time (spec §13).

On a `VirtualTimeLoop`, time only moves when every task is waiting on a timer:
the loop then jumps straight to the next timer. Sleeps and timeouts of any
length complete instantly and deterministically. It is meant for engine tests
that do no real I/O (the fake adapter, condition, sleep and set nodes); a real
child process would see time jump while it runs.
"""

from __future__ import annotations

import asyncio
import datetime
import selectors
from collections.abc import Coroutine
from typing import Any, TypeVar

from arcflow.clock import Clock

T = TypeVar("T")


class _JumpingSelector(selectors.BaseSelector):
    def __init__(self, real: selectors.BaseSelector, loop: VirtualTimeLoop) -> None:
        self._real = real
        self._loop = loop

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._real.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            return self._real.select(None)
        self._loop.virtual_time += timeout
        return []

    def close(self) -> None:
        self._real.close()

    def get_key(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.get_key(fileobj)

    def get_map(self) -> Any:
        return self._real.get_map()


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.virtual_time = 0.0
        super().__init__(selectors.DefaultSelector())
        self._selector = _JumpingSelector(self._selector, self)  # type: ignore[has-type]

    def time(self) -> float:
        return self.virtual_time


class VirtualClock(Clock):
    """Wall-clock time that follows the running loop's (virtual) time."""

    def __init__(self, start: datetime.datetime | None = None) -> None:
        self.start = start or datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def _loop_time(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return 0.0

    def now(self) -> datetime.datetime:
        return self.start + datetime.timedelta(seconds=self._loop_time())

    def monotonic(self) -> float:
        return self._loop_time()


def run_virtual(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion on a fresh virtual-time loop."""
    loop = VirtualTimeLoop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()
