"""The runner lock: one writer per run (spec §7.4).

`lock` is created with O_CREAT|O_EXCL and holds `{pid, host, started_at,
heartbeat_at}`. The holder refreshes `heartbeat_at` every few seconds. A lock is
stale when its host is this host and the PID is gone, or when its heartbeat is
older than 30 seconds. Taking over a stale lock renames it aside first, so two
processes racing to take it over cannot both win.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow.clock import Clock, iso, parse_iso

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
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                current = read_lock(self.path)
                if current is None:
                    continue  # released meanwhile
                if not is_stale(current, now):
                    raise LockHeld(current) from None
                aside = self.path.with_name(f"lock.stale.{os.getpid()}")
                try:
                    os.rename(self.path, aside)
                except FileNotFoundError:
                    continue  # someone else took it over first; try again
                aside.unlink(missing_ok=True)
                self.took_over = current
                continue
            with os.fdopen(fd, "w") as handle:
                json.dump(info.to_json(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.info = info
            return
        current = read_lock(self.path) or {}
        raise LockHeld(current)

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
