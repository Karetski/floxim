"""The append-only event log, `events.jsonl`.

Each line is one JSON event. Only the process holding the run lock appends. A
checkpoint event is flushed and `fsync`ed before the writer returns; progress
events are only flushed. A final line cut short by a crash is ignored (with a
warning) and trimmed before the next append; any other bad line, or a gap in
`seq`, is corruption.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.clock import Clock, iso

EVENT_VERSION = 1
BRANCH = "main"  # the only branch in v1

# Events that are flushed but not fsynced.
NON_DURABLE = frozenset({"progress"})

Event = dict[str, Any]


class CorruptLog(Exception):
    def __init__(self, path: Path, line: int, reason: str) -> None:
        super().__init__(f"{path}:{line}: {reason}; repair it with `floxim doctor`")
        self.path = path
        self.line = line
        self.reason = reason


@dataclass
class LogRead:
    events: list[Event]
    torn_tail: bool  # the last line was incomplete and was ignored
    valid_bytes: int  # length of the log up to the end of the last good event
    needs_newline: bool = False  # the last good event was written without its newline


def read_log(path: Path) -> LogRead:
    """Read every event. Raises CorruptLog for anything but a torn final line."""
    if not path.exists():
        return LogRead([], False, 0)
    data = path.read_bytes()
    events: list[Event] = []
    offset = 0
    lines = data.split(b"\n")
    for number, raw in enumerate(lines, start=1):
        last = number == len(lines)
        if last and raw == b"":
            break  # the file ends with a newline
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except ValueError:
            if last:  # no newline after it: a write cut short by a crash
                return LogRead(events, True, offset)
            raise CorruptLog(path, number, "invalid JSON") from None
        expected = len(events) + 1
        if event.get("seq") != expected:
            raise CorruptLog(path, number, f"seq {event.get('seq')} where {expected} was expected")
        events.append(event)
        offset += len(raw) + 1
        if last:
            # A complete event without its newline: keep it; the writer adds
            # the newline before appending.
            return LogRead(events, False, offset - 1, needs_newline=True)
    return LogRead(events, False, offset)


class EventWriter:
    """Appends events for the process that holds the run lock."""

    def __init__(
        self,
        path: Path,
        clock: Clock,
        on_event: Callable[[Event], None] | None = None,
        crash_hook: Callable[[Event], None] | None = None,
        redact: Callable[[Any], Any] | None = None,
    ) -> None:
        self.path = path
        self.clock = clock
        self.redact = redact
        self.on_event = on_event
        self.crash_hook = crash_hook
        read = read_log(path)
        self.events = read.events
        self.torn_tail = read.torn_tail
        if path.exists() and (path.stat().st_size != read.valid_bytes or read.needs_newline):
            with open(path, "r+b") as handle:
                handle.truncate(read.valid_bytes)
                if read.needs_newline:
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        self._handle = open(path, "ab")  # noqa: SIM115 - closed in close()

    @property
    def seq(self) -> int:
        return len(self.events)

    def append(
        self,
        type_: str,
        data: dict[str, Any] | None = None,
        *,
        node: str | None = None,
        visit: int | None = None,
        attempt: int | None = None,
    ) -> Event:
        event: Event = {
            "v": EVENT_VERSION,
            "seq": self.seq + 1,
            "ts": iso(self.clock.now()),
            "type": type_,
            "branch": BRANCH,
        }
        if node is not None:
            event["node"] = node
        if visit is not None:
            event["visit"] = visit
        if attempt is not None:
            event["attempt"] = attempt
        event["data"] = self.redact(data or {}) if self.redact else (data or {})
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._handle.write(line.encode("utf-8"))
        self._handle.flush()
        if type_ not in NON_DURABLE:
            os.fsync(self._handle.fileno())
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)
        if self.crash_hook is not None:
            self.crash_hook(event)
        return event

    def close(self) -> None:
        self._handle.close()
