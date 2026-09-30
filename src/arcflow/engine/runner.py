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
from arcflow.adapters import Adapter, AgentResult, Usage, UsageUpdate
from arcflow.adapters import registry as adapter_registry
from arcflow.clock import Clock, iso, parse_iso
from arcflow.config import Config
from arcflow.engine import budget, environment, workspaces
from arcflow.engine.human import (
    INBOX_POLL_S,
    Answer,
    InvalidAnswer,
    TerminalPrompt,
    WaitReleased,
    check_answer,
    deadline_passed,
)
from arcflow.engine.inputs import resolve_inputs
from arcflow.engine.nodes import AttemptResult, Executor, SleepError, VisitContext
from arcflow.engine.process import Stop
from arcflow.engine.registry import EXECUTORS
from arcflow.engine.routing import Decision, RoutingError, decide
from arcflow.expr import EvalError, ExprError
from arcflow.flow import Flow, Node, load_flow
from arcflow.flowspec import DEFAULT_TIMEOUTS, WORKSPACE_TYPES
from arcflow.redact import Redactor
from arcflow.rendering import render_config
from arcflow.store import inbox
from arcflow.store.events import Event, EventWriter, read_log
from arcflow.store.ids import new_run_id
from arcflow.store.lock import HEARTBEAT_S, RunLock, hostname, lock_state
from arcflow.store.rundir import RunDir, write_json_atomic
from arcflow.store.state import State, active_seconds, apply, reduce
from arcflow.templates import render_value
from arcflow.units import parse_duration
from arcflow.validate import Report, validate

DEFAULT_GRACE_S = 10.0
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


class FullPermissionsRefused(Exception):
    """`permissions: full` needs --allow-full or `allow_full: true` in config (§12.2)."""

    def __init__(self, nodes: list[str]) -> None:
        super().__init__(
            f"node(s) {', '.join(nodes)} use permissions: full; run with --allow-full "
            "or set allow_full: true in the project config"
        )
        self.nodes = nodes


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
    allow_full: bool = False,
) -> RunDir:
    """Validate the flow, resolve inputs, and create the run directory (§6.1 step 1).
    Raises FlowInvalid or InputError; nothing is created in that case."""
    report = validate(flow_path, config=config, workdir=workdir)
    if not report.ok() or report.flow is None:
        raise FlowInvalid(report)
    flow = report.flow
    full = [n.id for n in flow.nodes.values() if n.config.get("permissions") == "full"]
    if full and not (allow_full or config["allow_full"]):
        raise FullPermissionsRefused(full)
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
            "git_head": workspaces.head_commit(workdir, os.environ),
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


@dataclass
class ResumeOptions:
    """`arcflow resume` options (spec §7.6)."""

    reload: bool = False
    from_node: str | None = None
    rerun: bool = False
    force: bool = False
    recreate_workspaces: bool = False


class ResumeRefused(Exception):
    """The run cannot be resumed as asked; nothing was changed."""


class RunCancelled(Exception):
    """A cancel request arrived while the runner was waiting."""


class Detached(Exception):
    """The runner was asked to stop and left the run resumable."""


def crash_hook_from_env(environ: Mapping[str, str]) -> Callable[[Event], None] | None:
    """Fault injection for tests (spec §13): `ARCFLOW_TEST_CRASH_AT=<event type>:<n>`
    kills the process right after the n-th event of that type in the run's log is
    written, counting events earlier processes wrote."""
    spec = environ.get("ARCFLOW_TEST_CRASH_AT")
    if not spec:
        return None
    kind, _, count = spec.rpartition(":")
    target = int(count)
    seen = 0

    def hook(event: Event) -> None:
        nonlocal seen
        if event["type"] == kind:
            seen += 1
            if seen == target:
                os._exit(137)

    def seed(events: list[Event]) -> None:
        nonlocal seen
        seen = sum(1 for e in events if e["type"] == kind)

    hook.seed = seed  # type: ignore[attr-defined]
    return hook


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
        grace: float = DEFAULT_GRACE_S,
        resume: ResumeOptions | None = None,
        on_wait: str = "exit",
    ) -> None:
        self.run_dir = run
        self.config = config
        self.clock = clock
        self.on_event = on_event
        self.environ = dict(os.environ if environ is None else environ)
        self.heartbeat_enabled = heartbeat
        self.crash_hook = crash_hook
        self.grace = grace
        self.on_wait = on_wait
        self.options = resume or ResumeOptions()
        self.redactor = Redactor(config["redact"])
        self.meta = run.meta()
        self.state: State = reduce(read_log(run.events).events)
        self.flow: Flow = self._load_flow(self._snapshot_path())
        self.writer: EventWriter | None = None
        self.shutting_down = False
        self._current: tuple[VisitContext, asyncio.Future[AttemptResult], bool] | None = None
        self._adapters: dict[str, Adapter] = {}
        self.cancel: dict[str, Any] | None = None  # the cancel request being honoured

    def _snapshot_path(self) -> Path:
        relative = str(self.state["run"].get("snapshot") or self.meta["snapshot_flow"])
        return self.run_dir.path / relative

    @staticmethod
    def _load_flow(path: Path) -> Flow:
        flow, problems = load_flow(path)
        if flow is None:
            raise ResumeRefused(f"the run's flow snapshot no longer loads: {problems[:1]}")
        return flow

    # -- lifecycle ----------------------------------------------------------------

    def check_resumable(self) -> None:
        """Raise ResumeRefused if this run cannot continue as asked (spec §1.1, §7.6)."""
        status = self.state["status"]
        if status == "succeeded":
            raise ResumeRefused("the run succeeded; a succeeded run cannot be resumed")
        if status in ("failed", "cancelled") and not self.options.force:
            raise ResumeRefused(f"the run {status}; use --force to continue it anyway")
        if self.options.from_node and self.options.from_node not in self.flow.nodes:
            raise ResumeRefused(f"--from: no node named {self.options.from_node!r}")
        missing = [w for w in self.state["workspaces"].values() if not Path(w["path"]).is_dir()]
        if missing and not self.options.recreate_workspaces:
            names = ", ".join(w["name"] for w in missing)
            raise ResumeRefused(
                f"E-WORKSPACE-MISSING: worktree {names} no longer exists; "
                "use --recreate-workspaces to recreate it from its branch"
            )

    async def run(self) -> RunOutcome:
        if self.state["status"] != "pending":
            self.check_resumable()
        lock = RunLock(self.run_dir.lock, self.clock)
        lock.acquire()
        heartbeat: asyncio.Task[None] | None = None
        try:
            self.writer = EventWriter(
                self.run_dir.events,
                self.clock,
                on_event=self._on_event,
                crash_hook=self.crash_hook,
                redact=self.redactor.value if self.redactor else None,
            )
            self.state = reduce(self.writer.events)
            seed = getattr(self.crash_hook, "seed", None)
            if seed is not None:
                seed(self.writer.events)
            if self.writer.torn_tail:
                self.emit("warning", {"code": "W-TORN-LOG", "message": "ignored a torn last line"})
            if self.state["status"] == "pending":
                self.emit("run_started", {"pid": os.getpid(), "host": hostname()})
            else:
                kind = "runner_takeover" if lock.took_over else "runner_attached"
                self.emit(kind, {"pid": os.getpid(), "host": hostname(), "reason": "resume"})
            if self.heartbeat_enabled:
                heartbeat = asyncio.ensure_future(self._heartbeat(lock))
            watcher = asyncio.ensure_future(self._watch_inbox())
            try:
                status = await self._main()
            except RunCancelled:
                status = self._cancelled()
            except Detached:
                self.emit("runner_detached", {"pid": os.getpid(), "reason": "signal"})
                status = "detached" if self.state["status"] != "waiting" else "waiting"
            except WaitReleased:
                self.emit("runner_detached", {"pid": os.getpid(), "reason": "waiting"})
                status = "waiting"
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
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

    def request_shutdown(self, urgent: bool = False) -> None:
        """Stop the current attempt and leave the run resumable (spec §6.7)."""
        self.shutting_down = True
        if self._current is None:
            return
        ctx, task, handles_stop = self._current
        if handles_stop:
            ctx.stop.request("shutdown")
            if urgent:
                ctx.stop.urgent.set()
        else:
            task.cancel()

    async def _watch_inbox(self) -> None:
        """Take cancel requests from other processes while the run goes on (§7.4)."""
        while True:
            for path, request in inbox.pending(self.run_dir.inbox):
                if request.get("type") == "cancel":
                    inbox.consume(path)
                    self.request_cancel(request.get("by"), request.get("reason"))
            await self.clock.sleep(INBOX_POLL_S)

    def request_cancel(self, by: Any, reason: Any) -> None:
        """Stop the current attempt and end the run cancelled (spec §6.7)."""
        if self.cancel is not None:
            return
        self.cancel = {"by": by, "reason": reason}
        self.emit("cancel_requested", self.cancel)
        if self._current is None:
            return
        ctx, task, handles_stop = self._current
        if handles_stop:
            ctx.stop.request("cancel")
        else:
            task.cancel()

    def _cancelled(self) -> str:
        cancel = self.cancel or {}
        self.emit(
            "run_cancelled",
            {"by": cancel.get("by"), "reason": cancel.get("reason"), "totals": self._totals()},
        )
        self.run_dir.write_state(self.state)
        return "cancelled"

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

    async def _main(self) -> str:
        status = await self._recover()
        if status is not None:
            return status
        while True:
            if self.cancel is not None:
                return self._cancelled()
            if self.shutting_down:
                raise Detached
            current = self.state["current"] or self.flow.start
            if current == "end":
                return self._succeed()
            node = self.flow.nodes[current]
            limit = self._limit_reached(node)
            if limit is not None:
                return self._fail(limit[0], limit[1], node.id)
            outcome, decision = await self._visit(node)
            status = self._after_visit(node, outcome, decision)
            if status is not None:
                return status

    def _after_visit(self, node: Node, outcome: str, decision: Decision | None) -> str | None:
        """Route after a finished visit (§6.2 step 5). Returns a final status, or None."""
        if self.cancel is not None or outcome == "cancelled":
            return self._cancelled()  # cancellation is never routed (§6.5)
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
            message = decision.reason or f"{node.id} routed to fail"
            return self._fail("route_fail", message, node.id)
        return None

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
        if node.type == "agent":
            spent = self._run_budget_spent()
            if spent is not None:
                return "budget_exceeded", spent
        return None

    def max_duration(self) -> float | None:
        value = self.flow.limits.get("max_duration", DEFAULT_MAX_DURATION)
        return parse_duration(value)

    def remaining_active_seconds(self) -> float | None:
        limit = self.max_duration()
        if limit is None:
            return None
        return limit - active_seconds(self.state, iso(self.clock.now()))

    # -- resume (§7.5, §7.6) ----------------------------------------------------------

    async def _recover(self) -> str | None:
        """Bring a resumed run back to a step boundary. Returns a final status, or
        None to continue with the step loop."""
        options = self.options
        for recorded in list(self.state["workspaces"].values()):
            if not Path(recorded["path"]).is_dir():
                workspaces.recreate(recorded, self.workdir, self.git_env())
                self.emit("workspace_created", {**recorded, "recreated": True})
        if self.state["status"] in ("failed", "cancelled"):
            self._reopen()
        if options.reload:
            self._reload()
        progress = self.state["in_progress"]
        if options.from_node is not None:
            if progress is not None:
                self._close_interrupted(progress)
            self._jump(options.from_node)
            return None
        if progress is not None:
            node = self.flow.nodes[progress["node"]]
            outcome, decision = await self._continue_visit(node, progress)
            return self._after_visit(node, outcome, decision)
        awaiting = self.state["awaiting_route"]
        if awaiting is not None and self.state["current"] == awaiting:
            node = self.flow.nodes[awaiting]
            finished = self.state["nodes"][awaiting]
            decision = Decision(finished["branch"]) if node.type == "condition" else None
            return self._after_visit(node, finished["outcome"], decision)
        return None

    def _reopen(self) -> None:
        failure = self.state.get("failure") or {}
        previous = self.state["status"]
        self.emit("run_reopened", {"previous": previous})
        if self.options.from_node is None:
            target = failure.get("node")
            if target is None or target not in self.flow.nodes:
                raise ResumeRefused(f"the run {previous} outside a node; use --from <node>")
            self._jump(target)

    def _jump(self, target: str) -> None:
        self.emit(
            "route_taken",
            {"from": self.state["current"], "to": target, "via": "resume", "case_index": None},
        )

    def _reload(self) -> None:
        current_path = Path(self.meta["flow_path"])
        flow = self._load_flow(current_path)
        for node_id, result in self.state["nodes"].items():
            if result is None:
                continue
            new = flow.nodes.get(node_id)
            if new is None or new.type != self.flow.nodes[node_id].type:
                raise ResumeRefused(
                    f"--reload: node {node_id!r} has finished visits and was removed or "
                    "changed type"
                )
        generation = 1 + sum(1 for p in self.run_dir.path.glob("snapshot-*"))
        copy = self.run_dir.write_snapshot(flow.files(), flow.path, generation)
        old_sha = self.state["run"].get("flow_sha256") or self.meta["flow_sha256"]
        new_sha = hashlib.sha256(current_path.read_bytes()).hexdigest()
        self.emit(
            "flow_reloaded",
            {
                "old_sha256": old_sha,
                "new_sha256": new_sha,
                "snapshot": str(copy.relative_to(self.run_dir.path)),
            },
        )
        self.flow = self._load_flow(copy)

    def _close_interrupted(self, progress: dict[str, Any]) -> None:
        """Record an interrupted visit as finished without rerunning it (`--from`)."""
        node, visit = progress["node"], progress["visit"]
        attempts = progress["attempts"]
        if attempts and "outcome" not in attempts[-1]:
            self.emit(
                "attempt_finished",
                {"outcome": "interrupted", "error": None},
                node=node,
                visit=visit,
                attempt=attempts[-1]["attempt"],
            )
        result = {
            "outcome": "interrupted",
            "visit": visit,
            "attempts": len(attempts),
            "started_at": progress["started_at"],
            "finished_at": iso(self.clock.now()),
            "error": None,
        }
        self.emit(
            "visit_finished", {"outcome": "interrupted", "result": result}, node=node, visit=visit
        )

    async def _continue_visit(
        self, node: Node, progress: dict[str, Any]
    ) -> tuple[str, Decision | None]:
        """Finish a visit that started before the runner stopped (§7.5)."""
        executor = EXECUTORS[node.type]
        visit = progress["visit"]
        attempts = progress["attempts"]
        last = attempts[-1] if attempts else None
        if (
            getattr(executor, "continues_waiting", False)
            and not self.options.rerun
            and last is not None
            and "outcome" not in last
        ):
            return await self._continue_waiting(executor, node, progress, last)
        if last is not None and "outcome" not in last:
            self.emit(
                "attempt_finished",
                {"outcome": "interrupted", "error": None},
                node=node.id,
                visit=visit,
                attempt=last["attempt"],
            )
        used = sum(1 for a in attempts if a.get("outcome") not in (None, "interrupted"))
        mode = (
            "restart"
            if self.options.rerun
            else node.config.get("on_resume", executor.default_on_resume)
        )
        ctx = VisitContext(self, node, visit, {}, dict(progress.get("data") or {}))
        if mode == "ask":
            choice = await self._ask_after_interruption(ctx, progress)
            if choice == "skip":
                started = parse_iso(progress["started_at"])
                count = (last or {}).get("attempt", 0)
                return self._finish_visit(ctx, AttemptResult("succeeded", {}), count, started)
            if choice == "fail":
                return "failed", Decision("fail", None, f"{node.id} was interrupted; chose to fail")
            mode = "restart"
            ctx.visit_data = dict(progress.get("data") or {})
        ctx.workspace = progress.get("workspace")
        for earlier in attempts:  # spend of this visit before the interruption
            if isinstance(earlier.get("usage"), dict):
                ctx.spend.add(earlier.get("cost_usd"), int(earlier["usage"].get("total_tokens", 0)))
        ctx.namespace = self.namespace(node, visit, (last or {}).get("attempt", 0) + 1)
        if self.options.rerun:
            ctx.config = render_config(node.type, node.config, ctx.namespace, self.clock.now)
        else:
            config_path = self.run_dir.path / progress["config_ref"]
            ctx.config = json.loads(config_path.read_text())
        ctx.resume = {"mode": mode, "session_id": progress.get("session_id")}
        first = (last or {}).get("attempt", 0) + 1
        started = parse_iso(progress["started_at"])
        if last is not None and last.get("outcome") not in (None, "interrupted"):
            finished = _read_result(self.run_dir.attempt_dir(node.id, visit, last["attempt"]))
            if self._visit_ends(node, finished.outcome, used):
                return self._finish_visit(ctx, finished, last["attempt"], started)
        result, count = await self._attempts(executor, ctx, first=first, used=used)
        return self._finish_visit(ctx, result, count, started)

    async def _ask_after_interruption(self, ctx: VisitContext, progress: dict[str, Any]) -> str:
        """`on_resume: ask`: turn the interruption into a prompt (spec §7.5)."""
        node = ctx.node.id
        response = progress.get("response")
        if response is None:
            if node not in self.state["pending_human"]:
                self.emit(
                    "human_waiting",
                    {
                        "kind": "resume",
                        "message": f"{node} was interrupted. Rerun, skip, or fail?",
                        "choices": ["rerun", "skip", "fail"],
                        "input": "none",
                        "ack": False,
                        "show": [],
                        "default": None,
                        "deadline": None,
                    },
                    node=node,
                    visit=ctx.visit,
                )
                self.emit("run_waiting", {"nodes": sorted(self.state["pending_human"])})
                self.run_dir.write_state(self.state)
            ctx.visit_data = {}
            response = await self.wait_for_answer(ctx)
        return str(response.get("choice"))

    async def _continue_waiting(
        self, executor: Executor, node: Node, progress: dict[str, Any], last: dict[str, Any]
    ) -> tuple[str, Decision | None]:
        """Continue an open attempt of a node that waits (human, sleep): its wait
        goes on where it stopped, with no new attempt (spec §7.5)."""
        visit = progress["visit"]
        ctx = VisitContext(self, node, visit, {}, dict(progress.get("data") or {}))
        ctx.workspace = progress.get("workspace")
        ctx.namespace = self.namespace(node, visit, last["attempt"])
        ctx.config = json.loads((self.run_dir.path / progress["config_ref"]).read_text())
        ctx.attempt = last["attempt"]
        ctx.attempt_dir = self.run_dir.attempt_dir(node.id, visit, ctx.attempt)
        ctx.attempt_dir.mkdir(parents=True, exist_ok=True)
        started = parse_iso(progress["started_at"])
        result = await self._attempt(executor, ctx)
        write_json_atomic(ctx.attempt_dir / "result.json", _result_json(result))
        self.emit(
            "attempt_finished",
            {"outcome": result.outcome, "error": result.error, **result.extra},
            node=node.id,
            visit=visit,
            attempt=ctx.attempt,
        )
        if result.outcome == "interrupted":
            raise Detached
        used = (
            sum(1 for a in progress["attempts"] if a.get("outcome") not in (None, "interrupted"))
            + 1
        )
        if not self._visit_ends(node, result.outcome, used):
            result, count = await self._attempts(executor, ctx, first=ctx.attempt + 1, used=used)
            return self._finish_visit(ctx, result, count, started)
        return self._finish_visit(ctx, result, ctx.attempt, started)

    # -- human answers (§5.4, §6.11) -----------------------------------------------------

    async def wait_for_answer(self, ctx: VisitContext) -> dict[str, Any]:
        """Wait for an answer to the node's pending prompt: from the inbox (another
        process), from the terminal (`--on-wait prompt`), or by the deadline. With
        `--on-wait exit` the runner stops here and the run stays waiting."""
        node = ctx.node.id
        prompt = self.state["pending_human"][node]
        terminal = TerminalPrompt(prompt, node) if self.on_wait == "prompt" else None
        if terminal is not None:
            terminal.start()
        while True:
            response = self._take_answer(node, prompt, terminal)
            if response is not None:
                return response
            if deadline_passed(ctx.visit_data.get("deadline"), self.clock.now()):
                default = prompt.get("default")
                answer = Answer(choice=default, responder=None, via="timeout")
                return self._record_answer(node, answer)
            if self.cancel is not None:
                raise RunCancelled
            if self.on_wait == "exit":
                raise WaitReleased
            if ctx.stop.event.is_set() or self.shutting_down:
                raise Detached
            await self.clock.sleep(INBOX_POLL_S)

    def _take_answer(
        self, node: str, prompt: dict[str, Any], terminal: TerminalPrompt | None
    ) -> dict[str, Any] | None:
        for path, request in inbox.pending(self.run_dir.inbox):
            if request.get("type") != "respond" or request.get("node") != node:
                continue
            inbox.consume(path)
            answer = Answer(
                request.get("choice"),
                request.get("text"),
                bool(request.get("acknowledged")),
                request.get("responder"),
                str(request.get("via") or "cli"),
            )
            try:
                check_answer(prompt, answer)
            except InvalidAnswer as exc:
                self.emit("warning", {"code": "W-INVALID-ANSWER", "message": str(exc)}, node=node)
                continue
            return self._record_answer(node, answer)
        if terminal is not None and terminal.answer is not None:
            return self._record_answer(node, terminal.answer)
        return None

    def _record_answer(self, node: str, answer: Answer) -> dict[str, Any]:
        event = self.emit("human_responded", answer.to_json(), node=node)
        return {**answer.to_json(), "responded_at": event["ts"]}

    # -- visits and attempts --------------------------------------------------------------

    async def _visit(self, node: Node) -> tuple[str, Decision | None]:
        executor = EXECUTORS[node.type]
        visit = self.state["visits"].get(node.id, 0) + 1
        visit_dir = self.run_dir.visit_dir(node.id, visit)
        visit_dir.mkdir(parents=True, exist_ok=True)
        started = self.clock.now()
        self.run_dir.artifacts_dir(node.id, visit).mkdir(parents=True, exist_ok=True)
        ctx = VisitContext(self, node, visit, {}, {})
        failure: AttemptResult | None = None
        try:
            ctx.namespace = self.namespace(node, visit, 1)
            if node.type in ("human", "notify") and "message" in node.config:
                # `node.message` is available to the node's other fields (§4.3, §5.11).
                message = render_config(
                    node.type, {"message": node.config["message"]}, ctx.namespace, self.clock.now
                )["message"]
                ctx.namespace["node"]["message"] = message
            ctx.config = render_config(node.type, node.config, ctx.namespace, self.clock.now)
            ctx.workspace = self._workspace(node, visit, ctx.config)
            ctx.visit_data = executor.prepare(ctx)
        except (ExprError, EvalError, SleepError) as exc:
            failure = AttemptResult.failed("expression_error", str(exc))
        except workspaces.WorkspaceError as exc:
            failure = AttemptResult.failed("workspace_error", str(exc))
        config_text = json.dumps(ctx.config, indent=2, ensure_ascii=False, default=str)
        (visit_dir / "visit.json").write_text(config_text + "\n")
        self.emit(
            "visit_started",
            {
                "type": node.type,
                "config_ref": str((visit_dir / "visit.json").relative_to(self.run_dir.path)),
                "config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
                "workspace": ctx.workspace,
                **ctx.visit_data,
            },
            node=node.id,
            visit=visit,
        )
        if failure is not None:
            return self._finish_visit(ctx, failure, 0, started)
        result, count = await self._attempts(executor, ctx)
        return self._finish_visit(ctx, result, count, started)

    def _finish_visit(
        self, ctx: VisitContext, result: AttemptResult, attempts: int, started: Any
    ) -> tuple[str, Decision | None]:
        finished = self.clock.now()
        extra: dict[str, Any] = {}
        if ctx.node.type in WORKSPACE_TYPES:
            workspace = ctx.workspace or {"path": str(self.workdir), "branch": None}
            extra = {
                "workspace": {"path": workspace["path"], "branch": workspace["branch"]},
                "artifacts_dir": str(self.run_dir.artifacts_dir(ctx.node.id, ctx.visit)),
            }
        fields = {
            **result.fields,
            **extra,
            "outcome": result.outcome,
            "visit": ctx.visit,
            "attempts": attempts,
            "started_at": iso(started),
            "finished_at": iso(finished),
            "duration_s": round((finished - started).total_seconds(), 3),
            "error": result.error,
        }
        self.emit(
            "visit_finished",
            {"outcome": result.outcome, "result": fields},
            node=ctx.node.id,
            visit=ctx.visit,
        )
        return result.outcome, result.decision

    async def _attempts(
        self, executor: Executor, ctx: VisitContext, *, first: int = 1, used: int = 0
    ) -> tuple[AttemptResult, int]:
        """Run attempts until one succeeds or retries run out (§6.6). `used` counts
        earlier attempts that ended (interrupted ones do not count)."""
        node = ctx.node
        retry = node.config.get("retry") or {}
        max_attempts = int(retry.get("max_attempts", 1))
        backoff = parse_duration(retry.get("backoff", 0)) or 0.0
        result = AttemptResult.failed("limit", "no attempts left")
        attempt = first - 1
        while used < max_attempts:
            attempt += 1
            if used > 0:
                if backoff:
                    await self.clock.sleep(min(backoff * 2 ** (used - 1), BACKOFF_CAP_S))
                try:
                    ctx.namespace = self.namespace(node, ctx.visit, attempt)
                    ctx.config = render_config(
                        node.type, node.config, ctx.namespace, self.clock.now
                    )
                except (ExprError, EvalError) as exc:
                    result = AttemptResult.failed("expression_error", str(exc))
                    break
            if self.shutting_down:
                raise Detached
            ctx.attempt = attempt
            ctx.stop = Stop()
            ctx.scratch = {}
            ctx.attempt_dir = self.run_dir.attempt_dir(node.id, ctx.visit, attempt)
            ctx.attempt_dir.mkdir(parents=True, exist_ok=True)
            attempt_info = getattr(executor, "attempt_info", None)
            info = attempt_info(ctx) if attempt_info is not None else {}
            self.emit(
                "attempt_started",
                {"attempt": attempt, **info},
                node=node.id,
                visit=ctx.visit,
                attempt=attempt,
            )
            result = await self._attempt(executor, ctx)
            # The result file is written before the event that commits it, so a
            # resumed run can always finish the visit from a finished attempt.
            write_json_atomic(ctx.attempt_dir / "result.json", _result_json(result))
            self.emit(
                "attempt_finished",
                {"outcome": result.outcome, "error": result.error, **result.extra},
                node=node.id,
                visit=ctx.visit,
                attempt=attempt,
            )
            if result.outcome == "interrupted":
                raise Detached
            used += 1
            if self._visit_ends(node, result.outcome, used):
                break
        return result, attempt

    def _visit_ends(self, node: Node, outcome: str, used: int) -> bool:
        """Whether a visit ends after an attempt with `outcome`, `used` attempts in."""
        retry = node.config.get("retry") or {}
        if outcome == "succeeded" or outcome in NEVER_RETRIED:
            return True
        if outcome not in set(retry.get("on", RETRYABLE_DEFAULT)):
            return True
        return used >= int(retry.get("max_attempts", 1))

    async def _attempt(self, executor: Executor, ctx: VisitContext) -> AttemptResult:
        timeout = self.timeout_for(ctx.node, ctx.config)
        handles_stop = bool(getattr(executor, "handles_timeout", False))
        coroutine = executor.run(ctx)
        if timeout is not None and not handles_stop:
            coroutine = asyncio.wait_for(coroutine, timeout)
        task: asyncio.Future[AttemptResult] = asyncio.ensure_future(coroutine)
        self._current = (ctx, task, handles_stop)
        try:
            return await task
        except asyncio.TimeoutError:
            return AttemptResult(
                "timed_out", {}, {"kind": "timeout", "message": f"timed out after {timeout:g}s"}
            )
        except asyncio.CancelledError:
            if task.cancelled() and self.cancel is not None:
                return AttemptResult("cancelled")
            if self.shutting_down and task.cancelled():
                return AttemptResult("interrupted")
            raise
        except (ExprError, EvalError) as exc:
            return AttemptResult.failed("expression_error", str(exc))
        except RoutingError as exc:
            return AttemptResult.failed("expression_error", exc.message)
        finally:
            self._current = None

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
        self._remove_temporary_workspaces()
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

    # -- processes ------------------------------------------------------------------------

    def adapter(self, name: str) -> Adapter:
        """The run's adapter instance for a harness (one per runner process)."""
        if name not in self._adapters:
            adapter = adapter_registry.load(name, self.config.root)
            configure = getattr(adapter, "configure", None)
            if configure is not None:
                configure(self.config["harnesses"].get(name) or {})
            self._adapters[name] = adapter
        return self._adapters[name]

    # -- budgets (§6.8) ----------------------------------------------------------------

    def _cost(
        self, harness: str, model: str | None, usage: Usage, cost: float | None
    ) -> float | None:
        if cost is not None:
            return cost
        return budget.price_cost(self.config["prices"], harness, model, usage)

    def allowance(self, ctx: VisitContext) -> tuple[float | None, int | None]:
        """What the next adapter call may spend: the tighter of the node's and the
        run's remaining budget. Passed to the harness as a backstop."""
        node_usd, node_tokens = budget.node_limits(ctx.config)
        run_usd, run_tokens = budget.run_limits(self.flow.limits)
        totals = self.state["totals"]
        usd = [
            v
            for v in (
                None if node_usd is None else node_usd - ctx.spend.usd,
                None if run_usd is None else run_usd - totals["usd_spent"],
            )
            if v is not None
        ]
        tokens = [
            v
            for v in (
                None if node_tokens is None else node_tokens - ctx.spend.tokens,
                None if run_tokens is None else run_tokens - totals["tokens_spent"],
            )
            if v is not None
        ]
        return (
            max(min(usd), 0.0) if usd else None,
            int(max(min(tokens), 0)) if tokens else None,
        )

    def record_spend(
        self, ctx: VisitContext, harness: str, model: str | None, result: AgentResult
    ) -> float | None:
        """Add one adapter call's spend to the visit and the run (`budget_updated`)."""
        cost = self._cost(harness, model or result.model, result.usage, result.cost_usd)
        tokens = result.usage.total_tokens
        ctx.spend.add(cost, tokens)
        totals = self.state["totals"]
        usd_spent = round(totals["usd_spent"] + (cost or 0.0), 6)
        tokens_spent = totals["tokens_spent"] + tokens
        run_usd, run_tokens = budget.run_limits(self.flow.limits)
        self.emit(
            "budget_updated",
            {
                "usd_spent": usd_spent,
                "tokens_spent": tokens_spent,
                "usd_left": None if run_usd is None else round(run_usd - usd_spent, 6),
                "tokens_left": None if run_tokens is None else int(run_tokens - tokens_spent),
            },
            node=ctx.node.id,
            visit=ctx.visit,
            attempt=ctx.attempt,
        )
        return cost

    def on_usage(self, ctx: VisitContext, event: UsageUpdate) -> None:
        """Stop an agent attempt once its streamed usage crosses a budget."""
        harness = str(ctx.config.get("harness"))
        cost = self._cost(harness, ctx.config.get("model"), event.usage, event.cost_usd) or 0.0
        tokens = event.usage.total_tokens
        node_usd, node_tokens = budget.node_limits(ctx.config)
        run_usd, run_tokens = budget.run_limits(self.flow.limits)
        totals = self.state["totals"]
        crossed = (
            (node_usd is not None and ctx.spend.usd + cost > node_usd)
            or (node_tokens is not None and ctx.spend.tokens + tokens > node_tokens)
            or (run_usd is not None and totals["usd_spent"] + cost > run_usd)
            or (run_tokens is not None and totals["tokens_spent"] + tokens > run_tokens)
        )
        if crossed:
            ctx.stop.request("budget")

    def _run_budget_spent(self) -> str | None:
        run_usd, run_tokens = budget.run_limits(self.flow.limits)
        totals = self.state["totals"]
        if run_usd is not None and totals["usd_spent"] >= run_usd:
            return f"the run spent ~${totals['usd_spent']:.2f} of its ${run_usd:g} budget"
        if run_tokens is not None and totals["tokens_spent"] >= run_tokens:
            return f"the run used {totals['tokens_spent']} of its {int(run_tokens)} tokens"
        return None

    @property
    def workdir(self) -> Path:
        return Path(self.meta["workdir"])

    def node_workdir(self, ctx: VisitContext) -> Path:
        """Where a node's process runs: its worktree, else the run workdir (§6.9)."""
        return Path(ctx.workspace["path"]) if ctx.workspace else self.workdir

    def git_env(self) -> dict[str, str]:
        return environment.allowed(self.environ, passthrough=self.config["env_passthrough"])

    def _workspace(
        self, node: Node, visit: int, config: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The node's worktree, created on first use and reused after that."""
        if node.type not in WORKSPACE_TYPES:
            return None
        spec = workspaces.spec_for(node.id, config.get("workspace"))
        if spec is None:
            return None
        recorded = self.state["workspaces"].get(spec.name)
        if recorded is None:
            recorded = workspaces.create(
                spec,
                run_id=self.meta["id"],
                root=self.config.root,
                workdir=self.workdir,
                default_base=self.meta.get("git_head"),
                env=self.git_env(),
            )
            self.emit("workspace_created", recorded, node=node.id, visit=visit)
        return {"name": spec.name, "path": recorded["path"], "branch": recorded["branch"]}

    def _remove_temporary_workspaces(self) -> None:
        for recorded in list(self.state["workspaces"].values()):
            if recorded.get("keep", True) or not Path(recorded["path"]).is_dir():
                continue
            try:
                workspaces.remove(recorded, self.workdir, self.git_env())
            except workspaces.WorkspaceError as exc:
                self.emit("warning", {"code": "W-WORKSPACE-REMOVE", "message": str(exc)})
                continue
            self.emit("workspace_removed", {"name": recorded["name"], "path": recorded["path"]})

    def process_env(self, ctx: VisitContext, adapter_vars: tuple[str, ...] = ()) -> dict[str, str]:
        """The environment of a node's child process (spec §12.3)."""
        extra = {str(k): str(v) for k, v in (ctx.config.get("env") or {}).items()}
        artifacts = self.run_dir.artifacts_dir(ctx.node.id, ctx.visit)
        return environment.process_environment(
            self.environ,
            passthrough=self.config["env_passthrough"],
            adapter_vars=adapter_vars,
            extra=extra,
            arcflow={
                "ARCFLOW_RUN_ID": self.meta["id"],
                "ARCFLOW_RUN_DIR": str(self.run_dir.path),
                "ARCFLOW_NODE_ID": ctx.node.id,
                "ARCFLOW_VISIT": str(ctx.visit),
                "ARCFLOW_ATTEMPT": str(ctx.attempt),
                "ARCFLOW_ARTIFACTS_DIR": str(artifacts),
            },
        )

    # -- the expression namespace (§4.3) ----------------------------------------------------

    def allowed_env(self) -> dict[str, str]:
        return environment.allowed(self.environ, passthrough=self.config["env_passthrough"])

    def namespace(
        self, node: Node | None, visit: int | None, attempt: int | None, *, routing: bool = False
    ) -> dict[str, Any]:
        nodes = {node_id: self.state["nodes"].get(node_id) for node_id in self.flow.nodes}
        visits = {node_id: self.state["visits"].get(node_id, 0) for node_id in self.flow.nodes}
        if node is not None and visit is not None:
            visits[node.id] = max(visits[node.id], visit)  # counts the visit being started
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


def _result_json(result: AttemptResult) -> dict[str, Any]:
    decision = result.decision
    return {
        "outcome": result.outcome,
        "error": result.error,
        "fields": result.fields,
        "decision": None
        if decision is None
        else {
            "target": decision.target,
            "case_index": decision.case_index,
            "reason": decision.reason,
        },
    }


def _read_result(attempt_dir: Path) -> AttemptResult:
    data = json.loads((attempt_dir / "result.json").read_text())
    decision = Decision(**data["decision"]) if data.get("decision") else None
    return AttemptResult(data["outcome"], data["fields"], data["error"], decision=decision)


def display_status(run: RunDir, state: State, clock: Clock) -> str:
    """The status to show: a `running` run with no live runner is `interrupted` (§1.1)."""
    status = str(state.get("status"))
    if status == "running" and lock_state(run.lock, clock.now()) != "live":
        return "interrupted"
    return status


def exit_status_code(status: str) -> int:
    from arcflow.exitcodes import ExitCode

    return {
        "succeeded": ExitCode.OK,
        "failed": ExitCode.RUN_FAILED,
        "cancelled": ExitCode.CANCELLED,
        "waiting": ExitCode.WAITING,
        "detached": ExitCode.DETACHED,
    }[status]
