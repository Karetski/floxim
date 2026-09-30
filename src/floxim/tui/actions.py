"""Acting on runs from the TUI (spec §10.1): answering, running, cancelling,
resuming, opening sessions and artifacts. Every action goes through the same
library functions as the CLI, so it produces the same events (brief principle 2).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from floxim.clock import Clock
from floxim.config import Config
from floxim.engine.human import Answer, InvalidAnswer, check_answer, choice_values
from floxim.engine.inputs import InputError
from floxim.engine.respond import (
    AlreadyFinished,
    NotWaiting,
    cancel,
    respond,
    spawn_detached,
)
from floxim.engine.runner import FlowInvalid, FullPermissionsRefused, create_run
from floxim.flow import load_flow
from floxim.store.lock import LockHeld
from floxim.store.rundir import RunDir

_CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|[\x00-\x08\x0b-\x1f\x7f]"
)


def clean(text: str) -> str:
    """Text safe to show: terminal control sequences removed (spec §12.5). It is
    always displayed as plain text, so markup in it is never interpreted."""
    return _CONTROL.sub("", text)


def who() -> str | None:
    return os.environ.get("USER")


# -- answering ---------------------------------------------------------------------------


class AnswerModal(ModalScreen[str | None]):
    """A pending prompt: message, `show` lines, choices, and a text field when allowed."""

    DEFAULT_CSS = """
    AnswerModal { align: center middle; }
    #answer-box {
        width: 80%; height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #answer-choices { height: auto; }
    #answer-error { color: $error; height: auto; }
    """

    def __init__(self, config: Config, run: RunDir, node: str, prompt: dict[str, Any]) -> None:
        super().__init__()
        self.config = config
        self.run = run
        self.node = node
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        prompt = self.prompt
        with Vertical(id="answer-box"):
            yield Label(Text(f"{self.node} asks:", style="bold"))
            with VerticalScroll():
                yield Static(
                    Text(clean(str(prompt.get("message") or "")).strip()), id="answer-message"
                )
                for line in prompt.get("show") or []:
                    yield Static(Text(clean(str(line)), style="dim"))
            if prompt.get("input") == "text":
                yield Input(placeholder="your answer", id="answer-text")
            with Horizontal(id="answer-choices"):
                if prompt.get("ack"):
                    yield Button("Acknowledge", id="answer-ack", variant="primary")
                for value in choice_values(prompt.get("choices")):
                    yield Button(value, id=f"choice-{value}", classes="answer-choice")
                if prompt.get("input") == "text" and not prompt.get("choices"):
                    yield Button("Send", id="answer-send", variant="primary")
                yield Button("Cancel", id="answer-close")
            yield Static("", id="answer-error")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        button = event.button.id or ""
        if button == "answer-close":
            self.dismiss(None)
            return
        text_input = self.query("#answer-text")
        text = text_input.first(Input).value if text_input else None
        answer = Answer(
            choice=button.removeprefix("choice-") if button.startswith("choice-") else None,
            text=text or None,
            acknowledged=button == "answer-ack",
            responder=who(),
            via="tui",
        )
        try:
            check_answer(self.prompt, answer)
            respond(self.run, self.node, answer, clock=Clock(), project_root=self.config.root)
        except (InvalidAnswer, NotWaiting, LockHeld) as exc:
            self.query_one("#answer-error", Static).update(Text(str(exc)))
            return
        self.dismiss(answer.choice or answer.text or "ack")


# -- running a flow --------------------------------------------------------------------------


class RunModal(ModalScreen[str | None]):
    """A form built from the flow's inputs; starts a detached run (§10.1)."""

    DEFAULT_CSS = """
    RunModal { align: center middle; }
    #run-box {
        width: 70%; height: auto; max-height: 90%;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #run-error { color: $error; height: auto; }
    """

    def __init__(self, config: Config, path: Path) -> None:
        super().__init__()
        self.config = config
        self.path = path
        flow, _ = load_flow(path)
        self.inputs = flow.inputs if flow else {}

    def compose(self) -> ComposeResult:
        with Vertical(id="run-box"):
            yield Label(Text(f"Run {self.path.name}", style="bold"))
            for name, spec in self.inputs.items():
                required = " (required)" if spec.get("required") and "default" not in spec else ""
                yield Label(f"{name}: {spec['type']}{required}")
                default = spec.get("default")
                yield Input(
                    value="" if default is None else str(default),
                    placeholder=str(spec.get("description") or ""),
                    id=f"input-{name}",
                )
            with Horizontal():
                yield Button("Run", id="run-start", variant="primary")
                yield Button("Cancel", id="run-close")
            yield Static("", id="run-error")

    @on(Button.Pressed, "#run-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#run-start")
    def _start(self) -> None:
        given = {
            name: self.query_one(f"#input-{name}", Input).value
            for name in self.inputs
            if self.query_one(f"#input-{name}", Input).value != ""
        }
        try:
            run = create_run(
                self.path,
                given,
                config=self.config,
                clock=Clock(),
                workdir=self.config.root,
                from_text=set(given),
            )
        except FlowInvalid as exc:
            first = exc.report.errors[0].render() if exc.report.errors else "invalid flow"
            self.query_one("#run-error", Static).update(Text(first))
            return
        except (InputError, FullPermissionsRefused) as exc:
            self.query_one("#run-error", Static).update(Text(str(exc)))
            return
        spawn_detached(run, self.config.root)
        self.dismiss(run.id)


# -- run control -------------------------------------------------------------------------------


def cancel_run(config: Config, run: RunDir) -> str:
    try:
        return cancel(run, by=who(), reason="cancelled from the TUI", clock=Clock())
    except AlreadyFinished as exc:
        return str(exc)


def resume_run(config: Config, run: RunDir) -> str:
    status = run.read_state().get("status")
    if status in ("succeeded", "failed", "cancelled"):
        return f"the run {status}; resume it with `floxim resume --force` if you mean it"
    spawn_detached(run, config.root)
    return "resuming in the background"


def interactive(command: list[str], cwd: str | None = None) -> int:
    """Run an interactive command on the terminal (the caller suspends the TUI)."""
    return subprocess.run(command, cwd=cwd, check=False).returncode


def editor_command(path: str) -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return [*shlex.split(editor), path]
