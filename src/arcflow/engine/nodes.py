"""Node executors: what each node type does in one attempt (spec §5).

The runner owns everything common to all nodes: visits, attempts, retries,
timeouts, events and routing (§6.2). An executor only runs one attempt of its
node type and reports the result. Adding a node type means adding an
executor here; the runner does not change (brief principle 9).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from arcflow.clock import iso, parse_iso
from arcflow.engine.routing import Decision, decide
from arcflow.units import parse_duration

if TYPE_CHECKING:
    from arcflow.engine.runner import Runner
    from arcflow.flow import Node


@dataclass
class VisitContext:
    runner: Runner
    node: Node
    visit: int
    config: dict[str, Any]  # rendered for this attempt
    visit_data: dict[str, Any]  # recorded in visit_started (e.g. a sleep's wake_at)
    attempt: int = 1
    attempt_dir: Path | None = None
    namespace: dict[str, Any] = field(default_factory=dict)
    resume: dict[str, Any] | None = None  # set when continuing an interrupted visit


@dataclass
class AttemptResult:
    outcome: str
    fields: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    decision: Decision | None = None  # a condition node decides its own route
    extra: dict[str, Any] = field(default_factory=dict)  # recorded in attempt_finished

    @classmethod
    def failed(cls, kind: str, message: str, **fields: Any) -> AttemptResult:
        return cls("failed", fields, {"kind": kind, "message": message})


class Executor(Protocol):
    default_on_resume: str

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        """Visit-level data recorded in `visit_started`, computed once per visit."""
        ...

    async def run(self, ctx: VisitContext) -> AttemptResult: ...


class _Base:
    default_on_resume = "restart"

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}


class ConditionExecutor(_Base):
    """Routes without doing work (§5.3). Its visit succeeds unless an expression errors."""

    async def run(self, ctx: VisitContext) -> AttemptResult:
        decision = decide(ctx.node.config["next"], ctx.namespace, ctx.runner.clock.now)
        return AttemptResult("succeeded", {"branch": decision.target}, decision=decision)


class SleepExecutor(_Base):
    """Waits for a duration or until a time (§5.5). `wake_at` is recorded at visit
    start, so a resumed run sleeps only for the remainder."""

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        now = ctx.runner.clock.now()
        if "duration" in ctx.config:
            seconds = parse_duration(ctx.config["duration"])
            wake = now + datetime.timedelta(seconds=seconds or 0)
        else:
            until = ctx.config["until"]
            try:
                wake = parse_iso(str(until))
            except ValueError:
                raise SleepError(f"until: not an ISO 8601 time: {until!r}") from None
            if wake.tzinfo is None:
                wake = wake.replace(tzinfo=datetime.timezone.utc)
        return {"wake_at": iso(max(wake, now))}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        clock = ctx.runner.clock
        wake = parse_iso(ctx.visit_data["wake_at"])
        remaining = (wake - clock.now()).total_seconds()
        left = ctx.runner.remaining_active_seconds()
        if left is not None and remaining > left:
            return AttemptResult.failed(
                "limit", f"sleeping {remaining:.0f}s would pass limits.max_duration"
            )
        await clock.sleep(remaining)
        return AttemptResult("succeeded", {"woke_at": iso(clock.now())})


class SleepError(Exception):
    pass


class SetExecutor(_Base):
    """Assigns variables (§5.6). Values were rendered against the state before the
    node, at visit start; they are assigned together when the visit succeeds."""

    async def run(self, ctx: VisitContext) -> AttemptResult:
        return AttemptResult("succeeded", {"values": dict(ctx.config.get("vars") or {})})


EXECUTORS: dict[str, Executor] = {
    "condition": ConditionExecutor(),
    "sleep": SleepExecutor(),
    "set": SetExecutor(),
}


def register(node_type: str, executor: Executor) -> None:
    EXECUTORS[node_type] = executor
