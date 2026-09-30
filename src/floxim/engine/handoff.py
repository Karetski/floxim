"""The `handoff` node: hand an agent session to a person.

In the foreground with a person at the terminal (`--on-wait prompt`), Floxim
prints the message and runs the adapter's interactive command for the session,
attached to the terminal, and continues when it exits. Otherwise the run waits
like a human node, and `floxim handoff <run>` opens the session later.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from floxim.engine.nodes import AttemptResult, VisitContext


def handoff_command(ctx: VisitContext) -> tuple[str | None, list[str] | None, str]:
    """The session to hand over, the command that opens it, and where to run it."""
    runner = ctx.runner
    source = str(ctx.config["from"])
    latest = runner.state["nodes"].get(source) or {}
    session_id = latest.get("session_id")
    workspace = latest.get("workspace") or {}
    cwd = str(workspace.get("path") or runner.workdir)
    if session_id is None:
        return None, None, cwd
    harness = str(runner.flow.nodes[source].config.get("harness"))
    command = runner.adapter(harness).interactive_command(str(session_id), cwd)
    return str(session_id), command, cwd


class HandoffExecutor:
    default_on_resume = "resume"
    handles_timeout = True
    continues_waiting = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {"deadline": None}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        runner = ctx.runner
        session_id, command, cwd = handoff_command(ctx)
        if session_id is None or command is None:
            source = ctx.config["from"]
            return AttemptResult.failed("harness_error", f"{source} has no session to hand over")
        progress = runner.state.get("in_progress") or {}
        response = progress.get("response")
        if (
            response is None
            and runner.on_wait == "prompt"
            and ctx.node.id not in runner.state["pending_human"]
        ):
            print(
                f"\n→ {ctx.node.id}: {str(ctx.config.get('message') or '').strip()}",
                file=sys.stderr,
            )
            print(f"  opening: {' '.join(command)}", file=sys.stderr, flush=True)
            process = await asyncio.create_subprocess_exec(*command, cwd=cwd)
            code = await process.wait()
            return AttemptResult("succeeded", {"session_id": session_id, "exit_code": code})
        if response is None:
            if ctx.node.id not in runner.state["pending_human"]:
                runner.emit(
                    "human_waiting",
                    {
                        "kind": "handoff",
                        "message": str(ctx.config.get("message") or ""),
                        "choices": None,
                        "input": "none",
                        "ack": True,
                        "show": [f"session {session_id}: {' '.join(command)}"],
                        "default": None,
                        "deadline": None,
                        "session_id": session_id,
                        "command": command,
                    },
                    node=ctx.node.id,
                    visit=ctx.visit,
                )
                runner.emit("run_waiting", {"nodes": sorted(runner.state["pending_human"])})
                runner.run_dir.write_state(runner.state)
            response = await runner.wait_for_answer(ctx)
        return AttemptResult(
            "succeeded", {"session_id": session_id, "exit_code": response.get("exit_code")}
        )
