"""The runner lock: one writer per run.

`lock` holds `{pid, host, started_at, heartbeat_at}`. It is written aside and
hard-linked into place, which fails if it exists, so it never appears
half-written: a half-written lock would read as stale and invite a takeover.
The holder refreshes `heartbeat_at` every few seconds. A lock is stale when its
host is this host and the PID is gone, or when its heartbeat is older than 30
seconds. A takeover happens inside a short critical section guarded by an
exclusive `lock.takeover` marker, created the same way, and replaces the lock
only if it is still the stale lock that was judged, so two processes racing to
take it over cannot both win.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whisperwind.clock import Clock, iso, parse_iso

HEARTBEAT_S = 5.0
STALE_AFTER_S = 30.0


class LockHeld(Exception):
    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__(f"run is locked by pid {info.get('pid')} on {info.get('host')}")
        self.info = info


@dataclass
class LockInfo:
    pid: int
    host: str
    started_at: str
    heartbeat_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "host": self.host,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
        }


def _create_exclusive(path: Path, data: dict[str, Any]) -> bool:
    """Create `path` holding `data` unless it exists. The content is written to a
    file of this process's own first, then linked into place whole."""
    temp = path.with_name(f"{path.name}.new.{os.getpid()}")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temp, path)
    except FileExistsError:
        return False
    finally:
        temp.unlink(missing_ok=True)
    return True


def hostname() -> str:
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}  # unreadable: being written, or garbage; treated by is_stale
    return data if isinstance(data, dict) else {}


def is_stale(info: dict[str, Any], now: datetime.datetime) -> bool:
    heartbeat = info.get("heartbeat_at")
    if not isinstance(heartbeat, str):
        return True
    try:
        age = (now - parse_iso(heartbeat)).total_seconds()
    except ValueError:
        return True
    if age > STALE_AFTER_S:
        return True
    pid = info.get("pid")
    return info.get("host") == hostname() and isinstance(pid, int) and not _pid_alive(pid)


def lock_state(path: Path, now: datetime.datetime) -> str:
    """ "free", "live" or "stale"."""
    info = read_lock(path)
    if info is None:
        return "free"
    return "stale" if is_stale(info, now) else "live"


class RunLock:
    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock
        self.info: LockInfo | None = None
        self.took_over: dict[str, Any] | None = None

    def acquire(self) -> None:
        """Take the lock, taking over a stale one. Raises LockHeld if it is live."""
        now = self.clock.now()
        stamp = iso(now)
        info = LockInfo(os.getpid(), hostname(), stamp, stamp)
        for _ in range(50):
            if self._create(info):
                return
            current = read_lock(self.path)
            if current is None:
                continue  # released meanwhile
            if not is_stale(current, now):
                raise LockHeld(current)
            if self._take_over(current, info):
                return
            time.sleep(0.01)
        raise LockHeld(read_lock(self.path) or {})

    def _create(self, info: LockInfo) -> bool:
        if not _create_exclusive(self.path, info.to_json()):
            return False
        self.info = info
        return True

    def _take_over(self, stale: dict[str, Any], info: LockInfo) -> bool:
        marker = self.path.with_name("lock.takeover")
        if not _create_exclusive(marker, info.to_json()):
            holder = read_lock(marker)
            if holder is not None and is_stale(holder, self.clock.now()):
                marker.unlink(missing_ok=True)  # left behind by a process that died
            return False
        try:
            if read_lock(self.path) != stale:
                return False  # changed since it was judged stale: judge again
            self.path.unlink(missing_ok=True)
            if not self._create(info):
                return False
            self.took_over = stale
            return True
        finally:
            marker.unlink(missing_ok=True)

    def heartbeat(self) -> None:
        if self.info is None:
            return
        self.info.heartbeat_at = iso(self.clock.now())
        temp = self.path.with_name(f"lock.tmp.{os.getpid()}")
        temp.write_text(json.dumps(self.info.to_json()))
        os.replace(temp, self.path)

    def release(self) -> None:
        if self.info is None:
            return
        current = read_lock(self.path)
        if (
            current
            and current.get("pid") == self.info.pid
            and current.get("host") == self.info.host
        ):
            self.path.unlink(missing_ok=True)
        self.info = None
