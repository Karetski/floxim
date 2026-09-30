"""The `human` node and answers to it (spec §5.4, §6.11, ADR 0006)."""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcflow.clock import iso, parse_iso
from arcflow.engine.nodes import AttemptResult, VisitContext
from arcflow.units import parse_duration

if TYPE_CHECKING:
    from arcflow.engine.runner import Runner

HOOK_TIMEOUT_S = 30.0
INBOX_POLL_S = 1.0


class InvalidAnswer(Exception):
    """An answer that does not fit the pending prompt; nothing is recorded."""


class WaitReleased(Exception):
    """The runner stops at a human node and leaves the run waiting (`--on-wait exit`)."""


@dataclass(frozen=True)
class Answer:
    choice: str | None = None
    text: str | None = None
    acknowledged: bool = False
    responder: str | None = None
    via: str = "cli"

    def to_json(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "text": self.text,
            "acknowledged": self.acknowledged,
            "responder": self.responder,
            "via": self.via,
        }


def choice_values(choices: list[Any] | None) -> list[str]:
    return [str(c["value"]) if isinstance(c, dict) else str(c) for c in choices or []]


def check_answer(prompt: dict[str, Any], answer: Answer) -> None:
    """Raise InvalidAnswer unless the answer fits the prompt (spec §5.4)."""
    values = choice_values(prompt.get("choices"))
    if prompt.get("ack"):
        if not answer.acknowledged:
            raise InvalidAnswer("this prompt only needs an acknowledgement: use --ack")
        return
    if values:
        if answer.choice is None:
            raise InvalidAnswer(f"choose one of: {', '.join(values)} (--choice)")
        if answer.choice not in values:
            raise InvalidAnswer(f"{answer.choice!r} is not one of: {', '.join(values)}")
        if answer.text is not None and prompt.get("input") != "text":
            raise InvalidAnswer("this prompt takes no text")
        return
    if prompt.get("input") == "text" and not (answer.text or "").strip():
        raise InvalidAnswer("this prompt needs a text answer (--text)")


def prompt_of(config: dict[str, Any]) -> dict[str, Any]:
    """The prompt a person sees, as recorded in `human_waiting`."""
    choices = config.get("choices")
    input_mode = config.get("input", "none" if choices or config.get("ack") else "text")
    return {
        "message": str(config.get("message") or ""),
        "choices": choices,
        "input": input_mode,
        "ack": bool(config.get("ack", False)),
        "show": [str(line) for line in config.get("show") or []],
        "default": config.get("default"),
    }


class HumanExecutor:
    default_on_resume = "resume"
    handles_timeout = True
    continues_waiting = True  # resume continues the same attempt instead of a new one

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        timeout = ctx.config.get("timeout", "7d")
        seconds = parse_duration(timeout)
        if seconds is None:
            return {"deadline": None}
        deadline = ctx.runner.clock.now() + datetime.timedelta(seconds=seconds)
        return {"deadline": iso(deadline)}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        runner = ctx.runner
        node = ctx.node.id
        progress = runner.state.get("in_progress") or {}
        recorded = progress.get("response")
        if recorded is None and node not in runner.state["pending_human"]:
            await self._start_waiting(ctx)
        if recorded is None:
            recorded = await runner.wait_for_answer(ctx)
        timed_out = recorded.get("via") == "timeout"
        if timed_out and recorded.get("choice") is None:
            return AttemptResult(
                "timed_out",
                _fields(recorded, timed_out=True),
                {"kind": "timeout", "message": "nobody answered before the timeout"},
            )
        return AttemptResult("succeeded", _fields(recorded, timed_out=timed_out))

    async def _start_waiting(self, ctx: VisitContext) -> None:
        runner = ctx.runner
        prompt = prompt_of(ctx.config)
        where: dict[str, Any] = {"node": ctx.node.id, "visit": ctx.visit}
        runner.emit(
            "human_waiting", {**prompt, "deadline": ctx.visit_data.get("deadline")}, **where
        )
        runner.emit("run_waiting", {"nodes": sorted(runner.state["pending_human"])})
        runner.run_dir.write_state(runner.state)
        hook = ctx.config.get("on_wait") or runner.flow.on_wait or runner.config["on_wait"]
        if hook:
            await run_hook(runner, ctx, str(hook), prompt["message"])


def _fields(recorded: dict[str, Any], *, timed_out: bool) -> dict[str, Any]:
    return {
        "choice": recorded.get("choice"),
        "text": recorded.get("text"),
        "acknowledged": bool(recorded.get("acknowledged")),
        "responder": recorded.get("responder"),
        "responded_at": recorded.get("responded_at"),
        "via": recorded.get("via"),
        "timed_out": timed_out,
    }


async def run_hook(runner: Runner, ctx: VisitContext, command: str, message: str) -> None:
    """Run an `on_wait` hook in its own session with a 30 s timeout; its failure is
    recorded, never fatal (spec §5.4). Values arrive as environment variables."""
    env = runner.process_env(ctx)
    env.update(
        {
            "ARCFLOW_MESSAGE": message,
            "ARCFLOW_RESPOND_CMD": f"arcflow respond {runner.meta['id']} {ctx.node.id}",
        }
    )
    started = time.monotonic()
    exit_code: int | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            command,
            cwd=str(runner.workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            exit_code = await asyncio.wait_for(process.wait(), HOOK_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    except OSError:
        exit_code = None
    runner.emit(
        "hook_ran",
        {
            "hook": "on_wait",
            "exit_code": exit_code,
            "duration_s": round(time.monotonic() - started, 3),
        },
        node=ctx.node.id,
        visit=ctx.visit,
    )


class TerminalPrompt:
    """Asks on the terminal (`--on-wait prompt`) from a daemon thread, so an answer
    from another client can win without leaving a blocked read behind."""

    def __init__(self, prompt: dict[str, Any], node: str) -> None:
        self.prompt = prompt
        self.node = node
        self.answer: Answer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._ask, daemon=True)
        self._thread.start()

    def _ask(self) -> None:
        prompt = self.prompt
        print(f"\n? {self.node}: {prompt['message'].strip()}", file=sys.stderr)
        for line in prompt.get("show") or []:
            print(f"  {line}", file=sys.stderr)
        values = choice_values(prompt.get("choices"))
        while self.answer is None:
            if prompt.get("ack"):
                print("  press Enter to acknowledge: ", end="", file=sys.stderr, flush=True)
            elif values:
                print(f"  [{'/'.join(values)}]: ", end="", file=sys.stderr, flush=True)
            else:
                print("  answer: ", end="", file=sys.stderr, flush=True)
            line = sys.stdin.readline()
            if not line:
                return  # stdin closed: leave the answer to other clients
            reply = line.strip()
            candidate = (
                Answer(acknowledged=True)
                if prompt.get("ack")
                else Answer(choice=reply)
                if values
                else Answer(text=reply)
            )
            candidate = Answer(
                candidate.choice,
                candidate.text,
                candidate.acknowledged,
                os.environ.get("USER"),
                "cli",
            )
            try:
                check_answer(prompt, candidate)
            except InvalidAnswer as exc:
                print(f"  {exc}", file=sys.stderr)
                continue
            self.answer = candidate


def deadline_passed(deadline: str | None, now: datetime.datetime) -> bool:
    return deadline is not None and now >= parse_iso(deadline)
