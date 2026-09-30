"""Shared machinery for adapters that drive a harness CLI with JSON-lines output.

A process adapter builds a command line, feeds the prompt on stdin, saves the
raw stream to `stream.jsonl` byte for byte, turns each JSON line into adapter
events with its parser, and builds the result when the process ends. On
cancellation it runs the §6.7 stop sequence on the process group while still
reading the stream, so a final result that arrives during the grace period is
kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from floxim.adapters import (
    AdapterEvent,
    AgentRequest,
    AgentResult,
    Capabilities,
    ErrorInfo,
    Log,
    Probe,
)

DEFAULT_GRACE_S = 10.0
TERM_WAIT_S = 5.0


class StreamParser:
    """Parses one attempt's JSON lines. Subclasses keep whatever state they need."""

    def feed(self, event: dict[str, Any]) -> list[AdapterEvent]:
        raise NotImplementedError

    def finish(self, req: AgentRequest, exit_code: int | None, stderr_tail: str) -> AgentResult:
        raise NotImplementedError


def version_in_range(version: str, spec: str) -> bool:
    """Whether `version` satisfies a comma-separated range like ">=2.1.285,<2.2"."""

    def parts(text: str) -> tuple[int, ...]:
        return tuple(int(p) for p in re.findall(r"\d+", text)[:3])

    have = parts(version)
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        match = re.match(r"(>=|<=|==|>|<)?\s*(.+)", clause)
        assert match is not None
        op, want = match.group(1) or "==", parts(match.group(2))
        size = len(want)
        left = have[:size]
        ok = {
            ">=": left >= want,
            "<=": left <= want,
            ">": left > want,
            "<": left < want,
            "==": left == want,
        }[op]
        if not ok:
            return False
    return True


class ProcessAdapter:
    """Base class for tier 1 and tier 2 adapters that run a CLI."""

    name: ClassVar[str] = ""
    auth_env: ClassVar[tuple[str, ...]] = ()
    binary: ClassVar[str] = ""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}

    def configure(self, settings: dict[str, Any]) -> None:
        """Environment settings from config `harnesses.<name>` (§2.2)."""
        self.settings = dict(settings)

    @property
    def command_path(self) -> str:
        return str(self.settings.get("command") or self.binary)

    @property
    def grace(self) -> float:
        from floxim.units import parse_duration

        value = self.settings.get("grace")
        return float(parse_duration(value) or 0) if value is not None else DEFAULT_GRACE_S

    # -- to implement ------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def options_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"extra_args": {"type": "array", "items": {"type": "string"}}},
        }

    def command(self, req: AgentRequest) -> list[str]:
        raise NotImplementedError

    def stdin(self, req: AgentRequest) -> str | None:
        return req.prompt

    def parser(self, req: AgentRequest) -> StreamParser:
        raise NotImplementedError

    def early_events(self, req: AgentRequest) -> list[AdapterEvent]:
        """Events known before the process starts (a caller-chosen session ID)."""
        return []

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None:
        return None

    def version_command(self) -> list[str]:
        return [self.command_path, "--version"]

    # -- shared behaviour ----------------------------------------------------------

    async def probe(self) -> Probe:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.version_command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(process.communicate(), 20)
        except (OSError, asyncio.TimeoutError):
            return Probe(found=False)
        text = out.decode("utf-8", errors="replace").strip()
        match = re.search(r"\d+\.\d+(\.\d+)?", text)
        version = match.group(0) if match else (text or None)
        tested = self.settings.get("tested_versions")
        in_range = version_in_range(version, tested) if version and tested else None
        return Probe(found=True, version=version, in_tested_range=in_range)

    async def run(self, req: AgentRequest, emit: Callable[[AdapterEvent], None]) -> AgentResult:
        attempt_dir = Path(req.attempt_dir)
        parser = self.parser(req)
        for event in self.early_events(req):
            emit(event)
        argv = self.command(req) + [str(a) for a in req.options.get("extra_args", [])]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=req.cwd,
                env=req.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return AgentResult("failed", error=ErrorInfo("spawn_failed", f"{argv[0]}: {exc}"))
        reader = asyncio.ensure_future(
            _read_stream(process.stdout, attempt_dir / "stream.jsonl", parser, emit)
        )
        stderr_tail: list[bytes] = []
        errors = asyncio.ensure_future(
            _read_stderr(process.stderr, attempt_dir / "stderr.log", stderr_tail)
        )
        feeder = asyncio.ensure_future(_feed(process, self.stdin(req)))
        stopped_by: str | None = None
        try:
            await process.wait()
        except asyncio.CancelledError:
            stopped_by = await asyncio.shield(self._stop(process))
        await asyncio.gather(reader, errors, feeder, return_exceptions=True)
        tail = b"".join(stderr_tail).decode("utf-8", errors="replace")
        result = parser.finish(req, process.returncode, tail)
        if stopped_by is not None:
            result.stopped_by = stopped_by  # type: ignore[assignment]
            result = _stopped_outcome(req, result)
        version = getattr(parser, "version", None)
        tested = self.settings.get("tested_versions")
        if version and tested and not version_in_range(version, tested):
            emit(Log("warning", f"{self.name} {version} is outside tested_versions {tested}"))
        return result

    async def _stop(self, process: asyncio.subprocess.Process) -> str:
        for sig, wait, name in (
            (signal.SIGINT, self.grace, "sigint"),
            (signal.SIGTERM, TERM_WAIT_S, "sigterm"),
        ):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sig)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), wait)
                return name
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return "sigkill"


def _stopped_outcome(req: AgentRequest, result: AgentResult) -> AgentResult:
    """After Floxim stopped the harness, the outcome follows why it stopped it,
    keeping any usage and cost the harness reported (§6.7)."""
    if result.outcome == "succeeded":
        return result  # the result arrived before the stop took effect
    outcome = {"timeout": "timed_out", "budget": "budget_exceeded"}.get(
        req.stop_reason or "", "cancelled"
    )
    result.outcome = outcome  # type: ignore[assignment]
    return result


async def _read_stream(
    stream: asyncio.StreamReader | None,
    path: Path,
    parser: StreamParser,
    emit: Callable[[AdapterEvent], None],
) -> None:
    if stream is None:
        return
    pending = b""
    with open(path, "wb") as raw:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            raw.write(chunk)
            raw.flush()
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                _dispatch(line, parser, emit)
        if pending.strip():
            _dispatch(pending, parser, emit)


def _dispatch(line: bytes, parser: StreamParser, emit: Callable[[AdapterEvent], None]) -> None:
    if not line.strip():
        return
    try:
        event = json.loads(line)
    except ValueError:
        emit(Log("warning", f"non-JSON output: {line[:200].decode('utf-8', 'replace')}"))
        return
    if isinstance(event, dict):
        for adapter_event in parser.feed(event):
            emit(adapter_event)


async def _read_stderr(stream: asyncio.StreamReader | None, path: Path, tail: list[bytes]) -> None:
    if stream is None:
        return
    with open(path, "wb") as log:
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            log.write(chunk)
            tail.append(chunk)
            while sum(len(c) for c in tail) > 8192 and len(tail) > 1:
                tail.pop(0)


async def _feed(process: asyncio.subprocess.Process, text: str | None) -> None:
    if process.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        if text is not None:
            process.stdin.write(text.encode("utf-8"))
            await process.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.close()


def parse_lines(parser: StreamParser, lines: Sequence[str]) -> list[AdapterEvent]:
    """Feed recorded lines through a parser (offline fixtures, §8.8)."""
    events: list[AdapterEvent] = []
    for line in lines:
        _dispatch(line.encode("utf-8"), parser, events.append)
    return events
