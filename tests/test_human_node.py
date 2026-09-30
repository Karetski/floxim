"""The human node, waiting and answers (spec §5.4, §6.11, §7.4, ADR 0006)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow

from floxim.engine.human import Answer, InvalidAnswer, check_answer
from floxim.store.events import read_log
from floxim.store.ids import resolve_run
from floxim.store.rundir import RunDir

APPROVE = """name: approve
nodes:
  work:
    type: shell
    run: echo built
    next: approve
  approve:
    type: human
    message: "Built. Ship it?"
    show: ["Output: ${{ nodes.work.stdout }}"]
    choices: [ship, stop]
    next:
      - when: nodes.approve.choice == "ship"
        to: ship
      - to: end
  ship:
    type: shell
    run: echo shipped > shipped.txt
"""


def project(tmp_path: Path, flow: str, config: str = "") -> Path:
    (tmp_path / ".floxim").mkdir(exist_ok=True)
    (tmp_path / ".floxim" / "config.yaml").write_text(config)
    (tmp_path / "flow.yaml").write_text(flow)
    return tmp_path


def cli(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root / "x"),
        "USER": "ada",
    }
    return subprocess.run(
        [sys.executable, "-m", "floxim", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=60,
    )


def latest(root: Path) -> RunDir:
    runs = root / ".floxim" / "runs"
    return RunDir(runs / resolve_run(runs, "@last"))


def wait_for_status(run: RunDir, statuses: set[str], timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = run.read_state()
        if state["status"] in statuses and not (run.path / "lock").exists():
            return state
        time.sleep(0.1)
    raise AssertionError(f"run stayed {run.read_state()['status']}")


def test_given_on_wait_exit_when_answered_later_then_a_detached_runner_finishes(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, APPROVE)
    started = cli(root, "run", "flow.yaml", "--on-wait", "exit")
    run = latest(root)

    # When
    answered = cli(root, "respond", "@last", "--choice", "ship", "--json")

    # Then
    assert started.returncode == 4, started.stderr
    assert json.loads(answered.stdout)["data"] == {
        "run_id": run.id,
        "node": "approve",
        "delivered": "recorded",
        "continued": True,
    }
    state = wait_for_status(run, {"succeeded", "failed"})
    assert state["status"] == "succeeded", (run.path / "runner.log").read_text()
    assert state["nodes"]["approve"]["choice"] == "ship"
    assert state["nodes"]["approve"]["responder"] == "ada"
    assert (root / "shipped.txt").exists()
    waiting = next(e for e in read_log(run.events).events if e["type"] == "human_waiting")
    assert waiting["data"]["show"] == ["Output: built\n"]


def test_given_invalid_answer_when_responding_then_exits_2_and_nothing_changes(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, APPROVE)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    before = latest(root).events.read_bytes()

    # When
    result = cli(root, "respond", "@last", "--choice", "maybe")

    # Then
    assert result.returncode == 2
    assert "not one of: ship, stop" in result.stderr
    assert latest(root).events.read_bytes() == before


def test_given_run_not_waiting_when_responding_then_exits_6(tmp_path: Path) -> None:
    root = project(tmp_path, "name: quick\nnodes:\n  a: {type: set, vars: {x: 1}}\n")
    assert cli(root, "run", "flow.yaml").returncode == 0
    assert cli(root, "respond", "@last", "--ack").returncode == 6


def test_given_live_runner_waiting_when_answered_then_it_takes_the_answer_from_its_inbox(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, APPROVE)
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "XDG_CONFIG_HOME": str(root / "x")}
    runner = subprocess.Popen(
        [sys.executable, "-m", "floxim", "run", "flow.yaml", "--on-wait", "wait"],
        cwd=root,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        runs = list((root / ".floxim" / "runs").glob("*/events.jsonl"))
        if runs and "run_waiting" in runs[0].read_text():
            break
        time.sleep(0.05)

    # When
    answered = cli(root, "respond", "@last", "approve", "--choice", "stop", "--json")
    code = runner.wait(30)

    # Then
    assert json.loads(answered.stdout)["data"]["delivered"] == "inbox"
    assert code == 0
    assert latest(root).read_state()["nodes"]["approve"]["choice"] == "stop"


def test_given_terminal_prompt_when_a_valid_line_is_typed_then_it_is_the_answer(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, APPROVE)

    # When: an invalid line first, then a valid one
    result = cli(root, "run", "flow.yaml", "--on-wait", "prompt", stdin="maybe\nship\n")

    # Then
    assert result.returncode == 0, result.stderr
    assert "Built. Ship it?" in result.stderr
    assert "not one of" in result.stderr
    assert latest(root).read_state()["nodes"]["approve"]["choice"] == "ship"


@pytest.mark.parametrize(
    ("extra", "outcome", "status"),
    [("    default: stop\n", "succeeded", "succeeded"), ("", "timed_out", "failed")],
)
def test_given_timeout_when_nobody_answers_then_default_or_timed_out(
    tmp_path: Path, extra: str, outcome: str, status: str
) -> None:
    # Given
    flow = f"""name: late
nodes:
  ask:
    type: human
    message: Anyone?
    choices: [go, stop]
    timeout: 2h
{extra}"""

    # When
    result = run_flow(tmp_path, flow, on_wait="wait")

    # Then
    ask = result.state["nodes"]["ask"]
    assert (ask["outcome"], ask["timed_out"], ask["via"]) == (outcome, True, "timeout")
    assert result.status == status
    assert result.state["totals"]["active_s"] < 1  # waiting is not active time


def test_given_on_wait_hook_when_waiting_starts_then_it_gets_the_prompt_in_its_environment(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, APPROVE, config="on_wait: 'env | grep ^FLOXIM_ > hook-env.txt'\n")

    # When
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4

    # Then
    env = dict(line.split("=", 1) for line in (root / "hook-env.txt").read_text().splitlines())
    assert env["FLOXIM_MESSAGE"] == "Built. Ship it?"
    assert env["FLOXIM_NODE_ID"] == "approve"
    assert env["FLOXIM_RESPOND_CMD"].startswith("floxim respond ")
    hook = next(e for e in read_log(latest(root).events).events if e["type"] == "hook_ran")
    assert hook["data"]["exit_code"] == 0


@pytest.mark.parametrize(
    ("prompt", "answer", "ok"),
    [
        ({"choices": ["a", "b"]}, Answer(choice="a"), True),
        ({"choices": ["a", "b"]}, Answer(choice="c"), False),
        ({"choices": [{"value": "a", "label": "Yes"}]}, Answer(choice="a"), True),
        ({"choices": ["a"], "input": "text"}, Answer(choice="a", text="because"), True),
        ({"choices": ["a"]}, Answer(choice="a", text="because"), False),
        ({"input": "text"}, Answer(text="  "), False),
        ({"input": "text"}, Answer(text="ok"), True),
        ({"ack": True}, Answer(acknowledged=True), True),
        ({"ack": True}, Answer(choice="a"), False),
    ],
)
def test_given_prompt_when_answer_checked_then_only_fitting_answers_pass(
    prompt: dict[str, Any], answer: Answer, ok: bool
) -> None:
    if ok:
        check_answer(prompt, answer)
    else:
        with pytest.raises(InvalidAnswer):
            check_answer(prompt, answer)


A1 = (Path(__file__).parent / "flows" / "valid" / "implement-feature.yaml").read_text()


def test_given_appendix_a1_when_approval_is_answered_then_the_flow_finishes(tmp_path: Path) -> None:
    # Given: A.1 on the fake adapter, with test and merge commands that need no project
    flow = (
        A1.replace("harness: claude", "harness: fake")
        .replace("harness: codex", "harness: fake")
        .replace(
            'args: [git, merge, --no-ff, "${{ inputs.branch }}"]', "run: echo merged > merged.txt"
        )
        + "\n"
    )
    flow = flow.replace(
        "    permissions: read-only\n    prompt: |\n      Write an implementation plan",
        "    permissions: read-only\n    harness_options:\n      responses:\n"
        "        - output: {status: ready, summary: Add a form, steps: [form]}\n"
        "    prompt: |\n      Write an implementation plan",
    )
    root = project(tmp_path, flow)
    schemas = Path(__file__).parent / "flows" / "valid" / "schemas"
    (root / "schemas").mkdir()
    (root / "schemas" / "plan.json").write_text((schemas / "plan.json").read_text())

    # When
    started = cli(
        root, "run", "flow.yaml", "--on-wait", "exit",
        "--input", "feature=login", "--input", "branch=feature/login", "--input", "test_command=true",
    )  # fmt: skip
    answered = cli(root, "respond", "@last", "approve", "--choice", "merge")

    # Then
    assert started.returncode == 4, started.stderr
    assert answered.returncode == 0, answered.stderr
    run = latest(root)
    state = wait_for_status(run, {"succeeded", "failed"})
    assert state["status"] == "succeeded", (run.path / "runner.log").read_text()
    assert (root / "merged.txt").exists()
    visited = [e["node"] for e in read_log(run.events).events if e["type"] == "visit_started"]
    assert visited == ["plan", "implement", "test", "approve", "merge"]
