"""Child processes: process groups, output capture and the stop sequence (spec §6.7).

Every child runs in its own session, so its whole process tree can be signalled.
Stopping sends SIGINT to the group, waits up to the grace period for it to exit,
then SIGTERM, then after five more seconds SIGKILL; the result records which
step ended it. This is the only module with platform-specific process code
(spec §15 Q10).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

TERM_WAIT_S = 5.0
CHUNK = 64 * 1024
MAX_LINE = 1024 * 1024  # longer lines are written without waiting for their end


@dataclass
class ProcessResult:
    exit_code: int | None  # None when killed by a signal
    signal: str | None
    stdout: str  # the tail kept for state
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stopped_by: str | None = None  # "sigint", "sigterm" or "sigkill" when Arcflow stopped it
    stop_reason: str | None = None  # "timeout", "cancel" or "shutdown"
    spawn_error: str | None = None


class Tail:
    """Keeps the last `limit` bytes of a stream, cut at a line boundary when possible."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.truncated = False

    def add(self, data: bytes) -> None:
        self.chunks.append(data)
        self.size += len(data)
        while self.size > self.limit and self.chunks:
            self.truncated = True
            excess = self.size - self.limit
            first = self.chunks[0]
            if len(first) <= excess:
                self.chunks.popleft()
                self.size -= len(first)
            else:
                self.chunks[0] = first[excess:]
                self.size -= excess

    def text(self) -> str:
        data = b"".join(self.chunks)
        if self.truncated and b"\n" in data[:-1]:
            data = data[data.index(b"\n") + 1 :]
        return data.decode("utf-8", errors="replace")


@dataclass
class Stop:
    """Asks a running process to stop; `reason` says why."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self.event.set()


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: str | None,
    stdout_file: Path,
    stderr_file: Path,
    max_output: int,
    timeout: float | None,
    grace: float,
    stop: Stop,
    redact: Callable[[str], str] = lambda text: text,
) -> ProcessResult:
    """Run `argv` to completion, a timeout, or a stop request."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return ProcessResult(None, None, "", "", False, False, spawn_error=str(exc))

    tails = (Tail(max_output), Tail(max_output))
    pumps = [
        asyncio.ensure_future(_pump(process.stdout, stdout_file, tails[0], redact)),
        asyncio.ensure_future(_pump(process.stderr, stderr_file, tails[1], redact)),
    ]
    if stdin is not None:
        pumps.append(asyncio.ensure_future(_feed(process, stdin)))

    stopped_by: str | None = None
    reason: str | None = None
    waiter = asyncio.ensure_future(process.wait())
    stopper = asyncio.ensure_future(stop.event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter not in done:
            reason = stop.reason if stopper in done else "timeout"
            stopped_by = await _stop_group(process, waiter, grace)
    except asyncio.CancelledError:
        # The runner itself is being cancelled: stop the child before leaving.
        await asyncio.shield(_stop_group(process, waiter, grace))
        raise
    finally:
        stopper.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)
    code = process.returncode
    signame = None
    if code is not None and code < 0:
        signame = signal.Signals(-code).name
        code = None
    return ProcessResult(
        code,
        signame,
        redact(tails[0].text()),
        redact(tails[1].text()),
        tails[0].truncated,
        tails[1].truncated,
        stopped_by=stopped_by,
        stop_reason=reason,
    )


async def _stop_group(
    process: asyncio.subprocess.Process, waiter: asyncio.Future[int], grace: float
) -> str:
    steps = ((signal.SIGINT, grace, "sigint"), (signal.SIGTERM, TERM_WAIT_S, "sigterm"))
    for sig, wait, name in steps:
        _signal_group(process.pid, sig)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), wait)
            return name
    _signal_group(process.pid, signal.SIGKILL)
    await waiter
    return "sigkill"


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _pump(
    stream: asyncio.StreamReader | None, path: Path, tail: Tail, redact: Callable[[str], str]
) -> None:
    """Copy a stream to its log file and tail, redacting whole lines."""
    if stream is None:
        return
    pending = b""
    with open(path, "wb") as handle:

        def write(raw: bytes) -> None:
            data = redact(raw.decode("utf-8", errors="replace")).encode("utf-8")
            handle.write(data)
            handle.flush()
            tail.add(data)

        while True:
            chunk = await stream.read(CHUNK)
            if not chunk:
                break
            pending += chunk
            cut = pending.rfind(b"\n")
            if cut >= 0:
                write(pending[: cut + 1])
                pending = pending[cut + 1 :]
            elif len(pending) > MAX_LINE:
                write(pending)
                pending = b""
        if pending:
            write(pending)


async def _feed(process: asyncio.subprocess.Process, text: str) -> None:
    if process.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.close()
