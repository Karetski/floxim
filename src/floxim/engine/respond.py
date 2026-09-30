"""Acting on a run from another process: answers, cancels, timeouts.

With a live runner, a request goes into the run's inbox and the runner records
it. Without one, the acting process takes the lock, records the events itself,
and (for answers and timeouts) starts a detached runner to continue the run.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.clock import Clock, parse_iso
from floxim.engine.human import Answer, InvalidAnswer, check_answer
from floxim.store import inbox
from floxim.store.events import EventWriter
from floxim.store.ids import list_run_ids
from floxim.store.lock import LockHeld, RunLock, lock_state
from floxim.store.rundir import RunDir
from floxim.store.state import TERMINAL, reduce


class NotWaiting(Exception):
    """The run has no pending prompt for that node."""


@dataclass(frozen=True)
class Delivery:
    node: str
    via_inbox: bool  # a live runner will record it
    continued: bool  # a detached runner was started


def pending_node(run: RunDir, node: str | None) -> tuple[str, dict[str, Any]]:
    pending = run.read_state()["pending_human"]
    if not pending:
        raise NotWaiting(f"run {run.id} is not waiting for an answer")
    if node is None:
        if len(pending) > 1:
            raise NotWaiting(
                f"several prompts are pending ({', '.join(sorted(pending))}); name one"
            )
        node = next(iter(pending))
    if node not in pending:
        raise NotWaiting(
            f"{node!r} is not waiting for an answer (pending: {', '.join(sorted(pending))})"
        )
    return node, pending[node]


def respond(
    run: RunDir,
    node: str | None,
    answer: Answer,
    *,
    clock: Clock,
    project_root: Path,
    continue_run: bool = True,
) -> Delivery:
    """Record an answer. Raises NotWaiting or InvalidAnswer; nothing changes then.
    An answer to a subflow or map node waiting on its child goes to the child."""
    enforce_timeouts(run, clock=clock, project_root=project_root, continue_run=continue_run)
    node, prompt = pending_node(run, node)
    check_answer(dict(prompt), answer)
    if prompt.get("kind") == "child" and prompt.get("child_run"):
        child = RunDir(run.path.parent / str(prompt["child_run"]))
        if lock_state(child.lock, clock.now()) == "live":
            respond(
                child, str(prompt["child_node"]), answer, clock=clock, project_root=project_root
            )
            return Delivery(node, via_inbox=True, continued=False)
        respond(
            child,
            str(prompt["child_node"]),
            answer,
            clock=clock,
            project_root=project_root,
            continue_run=False,
        )
        answer = Answer(
            answer.choice, answer.text, answer.acknowledged, answer.responder, "forwarded"
        )
    if lock_state(run.lock, clock.now()) == "live":
        inbox.post(run.inbox, {"type": "respond", "node": node, **answer.to_json()}, clock)
        return Delivery(node, via_inbox=True, continued=False)
    lock = RunLock(run.lock, clock)
    lock.acquire()
    try:
        writer = EventWriter(run.events, clock)
        writer.append("human_responded", answer.to_json(), node=node)
        writer.close()
        run.write_state(reduce(writer.events))
    finally:
        lock.release()
    if continue_run:
        spawn_detached(run, project_root)
    return Delivery(node, via_inbox=False, continued=continue_run)


def spawn_detached(run: RunDir, project_root: Path, extra_args: list[str] | None = None) -> int:
    """Start `floxim resume <run> --on-wait exit` in its own session; its output
    goes to the run's runner.log. Returns the PID."""
    log = open(run.path / "runner.log", "ab")  # noqa: SIM115 - owned by the child
    argv = [sys.executable, "-m", "floxim", "resume", run.id, "--on-wait", "exit"]
    process = subprocess.Popen(
        [*argv, *(extra_args or [])],
        cwd=project_root,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    log.close()
    return process.pid


class AlreadyFinished(Exception):
    pass


def cancel(run: RunDir, *, by: str | None, reason: str | None, clock: Clock) -> str:
    """Cancel a run: "requested" when a live runner will stop it, "cancelled" when
    this process recorded it. Raises AlreadyFinished for a finished run."""
    state = run.read_state()
    if state["status"] in TERMINAL:
        raise AlreadyFinished(f"run {run.id} already {state['status']}")
    request = {"by": by, "reason": reason}
    if lock_state(run.lock, clock.now()) == "live":
        inbox.post(run.inbox, {"type": "cancel", **request}, clock)
        return "requested"
    for child in _open_children(run, state):
        with contextlib.suppress(AlreadyFinished):
            cancel(child, by=by, reason=reason, clock=clock)
    lock = RunLock(run.lock, clock)
    lock.acquire()
    try:
        writer = EventWriter(run.events, clock)
        writer.append("cancel_requested", request)
        totals = reduce(writer.events)["totals"]
        writer.append("run_cancelled", {**request, "totals": totals})
        writer.close()
        run.write_state(reduce(writer.events))
    finally:
        lock.release()
    return "cancelled"


def _open_children(run: RunDir, state: dict[str, object]) -> list[RunDir]:
    progress = state.get("in_progress") or {}
    assert isinstance(progress, dict)
    return [RunDir(run.path.parent / str(c["run_id"])) for c in progress.get("children") or []]


def enforce_timeouts(
    run: RunDir, *, clock: Clock, project_root: Path, continue_run: bool = True
) -> bool:
    """Apply a passed human-node deadline when no runner holds the run (lazy
    timeouts). Returns whether anything timed out."""
    state = run.read_state()
    if state["status"] != "waiting" or lock_state(run.lock, clock.now()) == "live":
        return False
    now = clock.now()
    due = [
        (node, prompt)
        for node, prompt in state["pending_human"].items()
        if prompt.get("deadline") and parse_iso(str(prompt["deadline"])) <= now
    ]
    if not due:
        return False
    lock = RunLock(run.lock, clock)
    try:
        lock.acquire()
    except LockHeld:
        return False
    try:
        writer = EventWriter(run.events, clock)
        for node, prompt in due:
            answer = Answer(choice=prompt.get("default"), via="timeout")
            writer.append("human_responded", answer.to_json(), node=node)
        writer.close()
        run.write_state(reduce(writer.events))
    finally:
        lock.release()
    if continue_run:
        spawn_detached(run, project_root)
    return True


def resume_due(runs_dir: Path, *, clock: Clock, project_root: Path) -> list[str]:
    """`floxim resume --due`: continue runs whose human timeout or sleep has passed
    and that no runner holds. Returns the run IDs started."""
    started: list[str] = []
    now = clock.now()
    for run_id in list_run_ids(runs_dir):
        run = RunDir(runs_dir / run_id)
        if enforce_timeouts(run, clock=clock, project_root=project_root):
            started.append(run_id)
            continue
        state = run.read_state()
        progress = state.get("in_progress") or {}
        wake = progress.get("wake_at")
        if (
            state["status"] == "running"
            and progress.get("type") == "sleep"
            and wake
            and parse_iso(str(wake)) <= now
            and lock_state(run.lock, now) != "live"
        ):
            spawn_detached(run, project_root)
            started.append(run_id)
    return started


__all__ = [
    "AlreadyFinished",
    "Answer",
    "Delivery",
    "InvalidAnswer",
    "NotWaiting",
    "cancel",
    "enforce_timeouts",
    "respond",
    "resume_due",
    "spawn_detached",
]
