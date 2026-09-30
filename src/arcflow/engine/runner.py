"""Creating runs and executing them: the run lifecycle and step loop (spec §6.1, §6.2).

Every transition is an event appended to the run's log before the runner moves
on, so every event boundary is a checkpoint (ADR 0012). The runner keeps the
derived state in memory by applying each event as it is written, and rewrites
`state.json` after each routing decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow import __version__
from arcflow.clock import Clock, iso
from arcflow.config import Config
from arcflow.engine import environment
from arcflow.engine.inputs import resolve_inputs
from arcflow.engine.nodes import EXECUTORS, AttemptResult, Executor, SleepError, VisitContext
from arcflow.engine.routing import Decision, RoutingError, decide
from arcflow.expr import EvalError, ExprError
from arcflow.flow import Flow, Node, load_flow
from arcflow.flowspec import DEFAULT_TIMEOUTS
from arcflow.rendering import render_config
from arcflow.store.events import Event, EventWriter
from arcflow.store.ids import new_run_id
from arcflow.store.lock import HEARTBEAT_S, RunLock, hostname
from arcflow.store.rundir import RunDir, write_json_atomic
from arcflow.store.state import State, active_seconds, apply, reduce
from arcflow.templates import render_value
from arcflow.units import parse_duration
from arcflow.validate import Report, validate

DEFAULT_MAX_VISITS = 10
DEFAULT_MAX_STEPS = 200
DEFAULT_MAX_DURATION = "8h"
BACKOFF_CAP_S = 600.0
RETRYABLE_DEFAULT = ("failed", "timed_out")
NEVER_RETRIED = frozenset({"budget_exceeded", "cancelled", "schema_invalid"})


class FlowInvalid(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__(f"{report.path}: flow is invalid")
        self.report = report


# -- creating runs ---------------------------------------------------------------


def create_run(
    flow_path: Path,
    given_inputs: dict[str, Any],
    *,
    config: Config,
    clock: Clock,
    workdir: Path,
    from_text: set[str] | None = None,
    parent: str | None = None,
) -> RunDir:
    """Validate the flow, resolve inputs, and create the run directory (§6.1 step 1).
    Raises FlowInvalid or InputError; nothing is created in that case."""
    report = validate(flow_path, config=config, workdir=workdir)
    if not report.ok() or report.flow is None:
        raise FlowInvalid(report)
    flow = report.flow
    inputs = resolve_inputs(flow, given_inputs, from_text=from_text or set())
    run_id = new_run_id(flow.name, clock.now())
    flow_sha = hashlib.sha256(flow.path.read_bytes()).hexdigest()
    run = RunDir.create(
        config.runs_dir,
        run_id,
        {
            "id": run_id,
            "flow": flow.name,
            "flow_path": str(flow.path.resolve()),
            "flow_sha256": flow_sha,
            "arcflow_version": __version__,
            "inputs": inputs,
            "workdir": str(workdir.resolve()),
            "created_at": iso(clock.now()),
            "parent": parent,
            "host": hostname(),
            "user": getpass.getuser(),
        },
    )
    snapshot_flow = run.write_snapshot(flow.files(), flow.path)
    meta = run.meta()
    meta["snapshot_flow"] = str(snapshot_flow.relative_to(run.path))
    write_json_atomic(run.run_json, meta)
    writer = EventWriter(run.events, clock)
    writer.append(
        "run_created",
        {
            "flow": flow.name,
            "flow_sha256": flow_sha,
            "inputs": inputs,
            "workdir": meta["workdir"],
            "arcflow_version": __version__,
            "parent": parent,
        },
    )
    writer.close()
    run.write_state(reduce(writer.events))
    return run


# -- running -----------------------------------------------------------------------


@dataclass
class RunOutcome:
    status: str  # succeeded | failed | cancelled | waiting | detached
    state: State


class Runner:
    def __init__(
        self,
        run: RunDir,
        config: Config,
        clock: Clock,
        *,
        on_event: Callable[[Event], None] | None = None,
        environ: Mapping[str, str] | None = None,
        heartbeat: bool = True,
        crash_hook: Callable[[Event], None] | None = None,
    ) -> None:
        self.run_dir = run
        self.config = config
        self.clock = clock
        self.on_event = on_event
        self.environ = dict(os.environ if environ is None else environ)
        self.heartbeat_enabled = heartbeat
        self.crash_hook = crash_hook
        self.meta = run.meta()
        flow, problems = load_flow(run.path / self.meta["snapshot_flow"])
        if flow is None:
            raise RuntimeError(f"the run's flow snapshot no longer loads: {problems[:1]}")
        self.flow: Flow = flow
        self.state: State = {}
        self.writer: EventWriter | None = None

    # -- lifecycle ----------------------------------------------------------------

    async def run(self) -> RunOutcome:
        lock = RunLock(self.run_dir.lock, self.clock)
        lock.acquire()
        heartbeat: asyncio.Task[None] | None = None
        try:
            self.writer = EventWriter(
                self.run_dir.events, self.clock, on_event=self._on_event, crash_hook=self.crash_hook
            )
            self.state = reduce(self.writer.events)
            if self.state["status"] == "pending":
                self.emit("run_started", {"pid": os.getpid(), "host": hostname()})
            else:
                kind = "runner_takeover" if lock.took_over else "runner_attached"
                self.emit(kind, {"pid": os.getpid(), "host": hostname(), "reason": "resume"})
            if self.heartbeat_enabled:
                heartbeat = asyncio.ensure_future(self._heartbeat(lock))
            status = await self._loop()
            self.run_dir.write_state(self.state)
            return RunOutcome(status, self.state)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if self.writer is not None:
                self.writer.close()
            lock.release()

    async def _heartbeat(self, lock: RunLock) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            lock.heartbeat()

    def _on_event(self, event: Event) -> None:
        apply(self.state, event)
        if self.on_event is not None:
            self.on_event(event)

    def emit(
        self,
        type_: str,
        data: dict[str, Any] | None = None,
        *,
        node: str | None = None,
        visit: int | None = None,
        attempt: int | None = None,
    ) -> Event:
        assert self.writer is not None
        return self.writer.append(type_, data, node=node, visit=visit, attempt=attempt)

    # -- the step loop (§6.2) ---------------------------------------------------------

    async def _loop(self) -> str:
        while True:
            current = self.state["current"] or self.flow.start
            if current == "end":
                return self._succeed()
            node = self.flow.nodes[current]
            limit = self._limit_reached(node)
            if limit is not None:
                return self._fail(limit[0], limit[1], node.id)
            outcome, decision = await self._visit(node)
            try:
                if decision is None:
                    decision = self._route(node, outcome)
            except RoutingError as exc:
                return self._fail(exc.reason, exc.message, node.id)
            if decision is None:  # on_error: fail
                error = (self.state["nodes"].get(node.id) or {}).get("error") or {}
                message = error.get("message") or f"{node.id} {outcome}"
                return self._fail("node_error", f"{node.id}: {message}", node.id)
            via = "next" if outcome == "succeeded" or self._continues(node) else "on_error"
            self.emit(
                "route_taken",
                {
                    "from": node.id,
                    "to": decision.target,
                    "via": via,
                    "case_index": decision.case_index,
                    "reason": decision.reason,
                },
            )
            self.run_dir.write_state(self.state)
            if decision.target == "fail":
                return self._fail(
                    "route_fail", decision.reason or f"{node.id} routed to fail", node.id
                )

    def _limit_reached(self, node: Node) -> tuple[str, str] | None:
        max_visits = int(node.config.get("max_visits", DEFAULT_MAX_VISITS))
        if self.state["visits"].get(node.id, 0) >= max_visits:
            return "max_visits_exceeded", f"{node.id} reached max_visits ({max_visits})"
        max_steps = self.flow.limits.get("max_steps", DEFAULT_MAX_STEPS)
        if max_steps != "none" and self.state["totals"]["steps"] >= int(max_steps):
            return "max_steps_exceeded", f"the run reached limits.max_steps ({max_steps})"
        left = self.remaining_active_seconds()
        if left is not None and left <= 0:
            return "max_duration_exceeded", "the run reached limits.max_duration"
        return None

    def max_duration(self) -> float | None:
        value = self.flow.limits.get("max_duration", DEFAULT_MAX_DURATION)
        return parse_duration(value)

    def remaining_active_seconds(self) -> float | None:
        limit = self.max_duration()
        if limit is None:
            return None
        return limit - active_seconds(self.state, iso(self.clock.now()))

    # -- visits and attempts --------------------------------------------------------------

    async def _visit(self, node: Node) -> tuple[str, Decision | None]:
        executor = EXECUTORS[node.type]
        visit = self.state["visits"].get(node.id, 0) + 1
        visit_dir = self.run_dir.visit_dir(node.id, visit)
        visit_dir.mkdir(parents=True, exist_ok=True)
        started = self.clock.now()
        ctx = VisitContext(self, node, visit, {}, {})
        render_error: str | None = None
        try:
            ctx.namespace = self.namespace(node, visit, 1)
            ctx.config = render_config(node.type, node.config, ctx.namespace, self.clock.now)
            ctx.visit_data = executor.prepare(ctx)
        except (ExprError, EvalError, SleepError) as exc:
            render_error = str(exc)
        config_text = json.dumps(ctx.config, indent=2, ensure_ascii=False, default=str)
        (visit_dir / "visit.json").write_text(config_text + "\n")
        self.emit(
            "visit_started",
            {
                "type": node.type,
                "config_ref": str((visit_dir / "visit.json").relative_to(self.run_dir.path)),
                "config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
                **ctx.visit_data,
            },
            node=node.id,
            visit=visit,
        )
        if render_error is not None:
            result = AttemptResult.failed("expression_error", render_error)
            attempts = 0
        else:
            result, attempts = await self._attempts(executor, ctx)
        finished = self.clock.now()
        fields = {
            **result.fields,
            "outcome": result.outcome,
            "visit": visit,
            "attempts": attempts,
            "started_at": iso(started),
            "finished_at": iso(finished),
            "duration_s": round((finished - started).total_seconds(), 3),
            "error": result.error,
        }
        self.emit(
            "visit_finished",
            {"outcome": result.outcome, "result": fields},
            node=node.id,
            visit=visit,
        )
        return result.outcome, result.decision

    async def _attempts(self, executor: Executor, ctx: VisitContext) -> tuple[AttemptResult, int]:
        node = ctx.node
        retry = node.config.get("retry") or {}
        max_attempts = int(retry.get("max_attempts", 1))
        retry_on = set(retry.get("on", RETRYABLE_DEFAULT))
        backoff = parse_duration(retry.get("backoff", 0)) or 0.0
        result = AttemptResult.failed("limit", "no attempt ran")
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                if backoff:
                    await self.clock.sleep(min(backoff * 2 ** (attempt - 2), BACKOFF_CAP_S))
                try:
                    ctx.namespace = self.namespace(node, ctx.visit, attempt)
                    ctx.config = render_config(
                        node.type, node.config, ctx.namespace, self.clock.now
                    )
                except (ExprError, EvalError) as exc:
                    result = AttemptResult.failed("expression_error", str(exc))
                    break
            ctx.attempt = attempt
            ctx.attempt_dir = self.run_dir.attempt_dir(node.id, ctx.visit, attempt)
            ctx.attempt_dir.mkdir(parents=True, exist_ok=True)
            self.emit(
                "attempt_started",
                {"attempt": attempt},
                node=node.id,
                visit=ctx.visit,
                attempt=attempt,
            )
            result = await self._attempt(executor, ctx)
            self.emit(
                "attempt_finished",
                {"outcome": result.outcome, "error": result.error, **result.extra},
                node=node.id,
                visit=ctx.visit,
                attempt=attempt,
            )
            write_json_atomic(
                ctx.attempt_dir / "result.json",
                {"outcome": result.outcome, "error": result.error, "fields": result.fields},
            )
            if result.outcome == "succeeded" or result.outcome in NEVER_RETRIED:
                break
            if result.outcome not in retry_on:
                break
        return result, attempt

    async def _attempt(self, executor: Executor, ctx: VisitContext) -> AttemptResult:
        timeout = self.timeout_for(ctx.node, ctx.config)
        try:
            if timeout is None:
                return await executor.run(ctx)
            return await asyncio.wait_for(executor.run(ctx), timeout)
        except asyncio.TimeoutError:
            return AttemptResult(
                "timed_out", {}, {"kind": "timeout", "message": f"timed out after {timeout:g}s"}
            )
        except (ExprError, EvalError) as exc:
            return AttemptResult.failed("expression_error", str(exc))
        except RoutingError as exc:
            return AttemptResult.failed("expression_error", exc.message)

    def timeout_for(self, node: Node, config: Mapping[str, Any]) -> float | None:
        value = config.get("timeout", DEFAULT_TIMEOUTS.get(node.type))
        return None if value is None else parse_duration(value)

    # -- routing and finishing ---------------------------------------------------------------

    def _continues(self, node: Node) -> bool:
        return bool(node.config.get("on_error", "fail") == "continue")

    def _route(self, node: Node, outcome: str) -> Decision | None:
        namespace = self.namespace(node, None, None, routing=True)
        if outcome == "succeeded" or self._continues(node):
            return decide(node.config.get("next", "end"), namespace, self.clock.now)
        on_error = node.config.get("on_error", "fail")
        if on_error == "fail":
            return None
        try:
            return decide(on_error, namespace, self.clock.now)
        except RoutingError as exc:
            if exc.reason == "no_route":
                return None  # a case list without a default falls back to fail (§6.5)
            raise

    def _succeed(self) -> str:
        try:
            namespace = self.namespace(None, None, None)
            outputs = render_value(self.flow.outputs, namespace, self.clock.now)
        except (ExprError, EvalError) as exc:
            return self._fail("expression_error", f"outputs: {exc}", None)
        self.emit("run_succeeded", {"outputs": outputs, "totals": self._totals()})
        self.run_dir.write_state(self.state)
        return "succeeded"

    def _fail(self, reason: str, message: str, node: str | None) -> str:
        self.emit(
            "run_failed",
            {"reason": reason, "node": node, "message": message, "totals": self._totals()},
        )
        self.run_dir.write_state(self.state)
        return "failed"

    def _totals(self) -> dict[str, Any]:
        totals = dict(self.state["totals"])
        totals["active_s"] = round(active_seconds(self.state, iso(self.clock.now())), 3)
        return totals

    # -- the expression namespace (§4.3) ----------------------------------------------------

    def allowed_env(self) -> dict[str, str]:
        return environment.allowed(self.environ, passthrough=self.config["env_passthrough"])

    def namespace(
        self, node: Node | None, visit: int | None, attempt: int | None, *, routing: bool = False
    ) -> dict[str, Any]:
        nodes = {node_id: self.state["nodes"].get(node_id) for node_id in self.flow.nodes}
        visits = {node_id: self.state["visits"].get(node_id, 0) for node_id in self.flow.nodes}
        totals = self.state["totals"]
        run = {
            "id": self.meta["id"],
            "flow": self.meta["flow"],
            "flow_file": self.meta["flow_path"],
            "started_at": self.state["run"].get("started_at"),
            "workdir": self.meta["workdir"],
            "budget": {
                "usd_spent": totals["usd_spent"],
                "tokens_spent": totals["tokens_spent"],
                "usd_left": totals["usd_left"],
                "tokens_left": totals["tokens_left"],
            },
        }
        namespace: dict[str, Any] = {
            "inputs": self.meta["inputs"],
            "nodes": nodes,
            "visits": visits,
            "vars": dict(self.state["vars"]),
            "run": run,
            "env": self.allowed_env(),
        }
        if node is not None and visit is not None:
            namespace["node"] = {
                "id": node.id,
                "visit": visit,
                "attempt": attempt,
                "artifacts_dir": str(self.run_dir.artifacts_dir(node.id, visit)),
                "workdir": self.meta["workdir"],
            }
        if node is not None and routing:
            namespace["self"] = nodes[node.id]
        return namespace


def exit_status_code(status: str) -> int:
    from arcflow.exitcodes import ExitCode

    return {
        "succeeded": ExitCode.OK,
        "failed": ExitCode.RUN_FAILED,
        "cancelled": ExitCode.CANCELLED,
        "waiting": ExitCode.WAITING,
        "detached": ExitCode.DETACHED,
    }[status]
