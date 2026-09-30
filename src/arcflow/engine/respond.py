"""Answering a waiting run from another process (spec §6.11, §7.4).

With a live runner, the answer goes into the run's inbox and the runner records
it. Without one, the answering process takes the lock, records the answer
itself, and starts a detached runner to continue the run, unless told not to.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from arcflow.clock import Clock
from arcflow.engine.human import Answer, InvalidAnswer, check_answer
from arcflow.store import inbox
from arcflow.store.events import EventWriter
from arcflow.store.lock import RunLock, lock_state
from arcflow.store.rundir import RunDir
from arcflow.store.state import reduce


class NotWaiting(Exception):
    """The run has no pending prompt for that node."""


@dataclass(frozen=True)
class Delivery:
    node: str
    via_inbox: bool  # a live runner will record it
    continued: bool  # a detached runner was started


def pending_node(run: RunDir, node: str | None) -> tuple[str, dict[str, object]]:
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
    """Record an answer. Raises NotWaiting or InvalidAnswer; nothing changes then."""
    node, prompt = pending_node(run, node)
    check_answer(dict(prompt), answer)
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
    """Start `arcflow resume <run> --on-wait exit` in its own session; its output
    goes to the run's runner.log. Returns the PID."""
    log = open(run.path / "runner.log", "ab")  # noqa: SIM115 - owned by the child
    argv = [sys.executable, "-m", "arcflow", "resume", run.id, "--on-wait", "exit"]
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


__all__ = ["Answer", "Delivery", "InvalidAnswer", "NotWaiting", "respond", "spawn_detached"]
