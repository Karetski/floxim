"""Run control and human prompts from the TUI."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_human_node import cli, project
from test_tui import drive, text_of
from textual.widgets import Input, Static

from whisperwind.config import load_config
from whisperwind.store.events import read_log
from whisperwind.store.ids import resolve_run
from whisperwind.store.rundir import RunDir
from whisperwind.tui import actions
from whisperwind.tui.app import WhisperwindApp
from whisperwind.tui.screens import RunDetailScreen

ASK = """name: ask
nodes:
  approve:
    type: human
    message: "Ship [bold]it[/]? \\x1b[31mnow\\x1b[0m"
    choices: [ship, stop]
    next: done
  done:
    type: set
    vars: {shipped: "${{ nodes.approve.choice }}"}
"""
HELLO = """name: hello
inputs:
  who: {type: string, required: true}
nodes:
  greet:
    type: set
    vars: {said: "hello ${{ inputs.who }}"}
outputs:
  said: ${{ vars.said }}
"""


def run_dir(root: Path) -> RunDir:
    runs = root / ".whisperwind" / "runs"
    return RunDir(runs / resolve_run(runs, "@last"))


def wait_until(check: Any, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def waiting(tmp_path: Path) -> Path:
    root = project(tmp_path, ASK)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    return root


def test_given_prompt_when_answered_in_the_tui_then_it_records_what_respond_would(
    waiting: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a twin project answered from the CLI, for comparison, by the same person
    monkeypatch.setenv("USER", "ada")
    (tmp_path / "twin").mkdir()
    twin = project(tmp_path / "twin", ASK)
    assert cli(twin, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    assert cli(twin, "respond", "@last", "--choice", "ship").returncode == 0
    config, _ = load_config(waiting)
    run = run_dir(waiting)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen)
        await pilot.press("a")
        await pilot.pause()
        await pilot.click("#choice-ship")
        await pilot.pause()

    # When
    drive(WhisperwindApp(config, target=run.id), scenario)

    # Then
    wait_until(lambda: run.read_state()["status"] == "succeeded" and not run.lock.exists())
    wait_until(
        lambda: (
            run_dir(twin).read_state()["status"] == "succeeded" and not run_dir(twin).lock.exists()
        )
    )

    def answer_of(of: RunDir) -> dict[str, Any]:
        return dict(
            next(e for e in read_log(of.events).events if e["type"] == "human_responded")["data"]
        )

    ours, theirs = answer_of(run), answer_of(run_dir(twin))
    assert ours["via"] == "tui" and theirs["via"] == "cli"
    assert {k: v for k, v in ours.items() if k != "via"} == {
        k: v for k, v in theirs.items() if k != "via"
    }
    types = [e["type"] for e in read_log(run.events).events]
    twin_types = [e["type"] for e in read_log(run_dir(twin).events).events]
    assert types == twin_types


def test_given_prompt_with_markup_and_escapes_when_shown_then_they_are_inert(waiting: Path) -> None:
    config, _ = load_config(waiting)

    async def scenario(pilot: Any) -> None:
        banner = text_of(pilot.app.screen.query_one("#banner", Static))
        assert "Ship [bold]it[/]? now" in banner
        assert "\x1b" not in banner

    drive(WhisperwindApp(config, target=run_dir(waiting).id), scenario)


def test_given_waiting_run_when_cancelled_from_the_runs_list_then_it_ends_cancelled(
    waiting: Path,
) -> None:
    config, _ = load_config(waiting)

    async def scenario(pilot: Any) -> None:
        await pilot.press("c")
        await pilot.pause()

    drive(WhisperwindApp(config), scenario)
    assert run_dir(waiting).read_state()["status"] == "cancelled"


def test_given_interrupted_run_when_resumed_from_the_tui_then_it_finishes(tmp_path: Path) -> None:
    # Given
    root = project(tmp_path, HELLO)
    env = {
        **os.environ,
        "WHISPERWIND_TEST_CRASH_AT": "visit_started:1",
        "XDG_CONFIG_HOME": str(root / "x"),
    }
    subprocess.run(
        [sys.executable, "-m", "whisperwind", "run", "flow.yaml", "--input", "who=ada"],
        cwd=root, env=env, capture_output=True,
    )  # fmt: skip
    config, _ = load_config(root)
    run = run_dir(root)

    async def scenario(pilot: Any) -> None:
        await pilot.press("u")
        await pilot.pause()

    # When
    drive(WhisperwindApp(config, target=run.id), scenario)

    # Then
    wait_until(lambda: run.read_state()["status"] == "succeeded")
    assert run.read_state()["outputs"] == {"said": "hello ada"}


def test_given_flow_when_run_from_the_flows_screen_then_the_form_supplies_inputs(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, HELLO)
    (root / "flows").mkdir()
    (root / "flows" / "hello.yaml").write_text(HELLO)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        pilot.app.screen.query_one("#input-who", Input).value = "grace"
        await pilot.click("#run-start")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RunDetailScreen)

    # When
    drive(WhisperwindApp(config), scenario)

    # Then
    run = run_dir(root)
    wait_until(lambda: run.read_state()["status"] == "succeeded")
    assert run.read_state()["outputs"] == {"said": "hello grace"}


def test_given_handoff_when_opened_from_the_tui_then_the_session_runs_and_the_flow_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    flow = """name: hand
nodes:
  work: {type: agent, harness: fake, prompt: Go., next: take}
  take: {type: handoff, from: work, message: Yours.}
"""
    root = project(tmp_path, flow)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    config, _ = load_config(root)
    run = run_dir(root)
    opened: list[list[str]] = []

    def fake_interactive(command: list[str], cwd: str | None = None) -> int:
        opened.append(command)
        return 0

    monkeypatch.setattr(actions, "interactive", fake_interactive)
    monkeypatch.setattr("whisperwind.tui.screens.interactive", fake_interactive)
    monkeypatch.setattr(WhisperwindApp, "suspend", lambda self: contextlib.nullcontext())

    async def scenario(pilot: Any) -> None:
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("y")  # copy the selected visit's session ID
        await pilot.pause()
        assert pilot.app.clipboard == run.read_state()["nodes"]["work"]["session_id"]

    # When
    drive(WhisperwindApp(config, target=run.id), scenario)

    # Then
    assert opened and opened[0][:2] == ["sh", "-c"]
    wait_until(lambda: run.read_state()["status"] == "succeeded")
    assert run.read_state()["nodes"]["take"]["exit_code"] == 0
