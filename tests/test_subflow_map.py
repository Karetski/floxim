"""Subflow and map nodes: child runs (spec §5.8, §5.9)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from engine_support import run_flow
from test_human_node import cli, project, wait_for_status

from arcflow.store.events import read_log
from arcflow.store.rundir import RunDir
from arcflow.validate import validate

CHILD = """name: child
inputs:
  word: {type: string, required: true}
nodes:
  shout:
    type: set
    vars: {loud: "${{ upper(inputs.word) }}"}
    next:
      - when: inputs.word == "boom"
        to: fail
        reason: exploded
      - to: end
outputs:
  loud: ${{ vars.loud }}
"""


def test_given_subflow_when_run_then_the_child_run_is_linked_and_its_outputs_returned(
    tmp_path: Path,
) -> None:
    # Given
    flow = """name: parent
nodes:
  call:
    type: subflow
    flow: child.yaml
    inputs: {word: hello}
outputs:
  loud: ${{ nodes.call.output.loud }}
"""

    # When
    result = run_flow(tmp_path, flow, files={"child.yaml": CHILD})

    # Then
    assert result.status == "succeeded", result.state["failure"]
    assert result.state["outputs"] == {"loud": "HELLO"}
    call = result.state["nodes"]["call"]
    child = RunDir(result.run.path.parent / call["run_id"])
    assert child.meta()["parent"] == result.run.id
    assert child.meta()["depth"] == 1
    assert next(e for e in result.events if e["type"] == "child_run")["data"] == {
        "run_id": child.id
    }


def test_given_failing_child_when_run_then_the_node_fails_and_can_be_routed(tmp_path: Path) -> None:
    # Given
    flow = """name: parent
nodes:
  call:
    type: subflow
    flow: child.yaml
    inputs: {word: boom}
    on_error: recover
  recover:
    type: set
    vars: {why: "${{ nodes.call.error.message }}"}
"""

    # When
    result = run_flow(tmp_path, flow, files={"child.yaml": CHILD})

    # Then
    assert result.status == "succeeded"
    call = result.state["nodes"]["call"]
    assert (call["outcome"], call["error"]["kind"], call["status"]) == (
        "failed",
        "child_failed",
        "failed",
    )
    assert "exploded" in result.state["vars"]["why"]


@pytest.mark.parametrize(
    ("on_item_error", "status", "counts"),
    [("continue", "succeeded", (2, 1)), ("fail", "failed", (1, 1))],
)
def test_given_map_when_an_item_fails_then_on_item_error_decides(
    tmp_path: Path, on_item_error: str, status: str, counts: tuple[int, int]
) -> None:
    # Given
    flow = f"""name: many
nodes:
  each:
    type: map
    items: ["a", "boom", "c"]
    flow: child.yaml
    inputs: {{word: "${{{{ item }}}}"}}
    on_item_error: {on_item_error}
outputs:
  louds: ${{{{ pluck(nodes.each.results, "outputs") }}}}
"""

    # When
    result = run_flow(tmp_path, flow, files={"child.yaml": CHILD})

    # Then
    assert result.status == status
    each = result.state["nodes"]["each"]
    assert (each["succeeded"], each["failed"]) == counts
    assert [r["index"] for r in each["results"]] == list(range(sum(counts)))
    if on_item_error == "continue":
        assert result.state["outputs"]["louds"] == [{"loud": "A"}, None, {"loud": "C"}]


def test_given_map_over_too_many_items_when_run_then_it_fails_before_starting(
    tmp_path: Path,
) -> None:
    flow = """name: many
nodes:
  each: {type: map, items: [1, 2, 3], max_items: 2, flow: child.yaml, inputs: {word: x}}
"""
    result = run_flow(tmp_path, flow, files={"child.yaml": CHILD})
    assert result.state["nodes"]["each"]["error"]["kind"] == "limit"
    assert "child_run" not in result.types()


def test_given_parent_budget_when_child_spends_then_the_parent_pays_and_the_child_is_capped(
    tmp_path: Path,
) -> None:
    # Given
    child = """name: spender
nodes:
  work:
    type: agent
    harness: fake
    prompt: Work.
    harness_options: {responses: [{cost_usd: 0.4}]}
"""
    flow = "name: parent\nlimits: {budget: {usd: 1}}\nnodes:\n  call: {type: subflow, flow: child.yaml}\n"

    # When
    result = run_flow(tmp_path, flow, files={"child.yaml": child})

    # Then
    assert result.state["totals"]["usd_spent"] == pytest.approx(0.4)
    child_run = RunDir(result.run.path.parent / result.state["nodes"]["call"]["run_id"])
    assert child_run.meta()["limits_cap"]["usd"] == pytest.approx(1.0)


def test_given_invalid_child_wiring_when_validated_then_inputs_are_checked(tmp_path: Path) -> None:
    (tmp_path / "child.yaml").write_text(CHILD)
    (tmp_path / "flow.yaml").write_text(
        "name: p\nnodes:\n  call: {type: subflow, flow: child.yaml, inputs: {wrod: x}}\n"
    )
    report = validate(tmp_path / "flow.yaml", implementation_gate=False)
    assert sorted(p.code for p in report.errors) == ["E-SCHEMA", "E-UNKNOWN-REF"]


ASKING_CHILD = """name: asker
nodes:
  ask:
    type: human
    message: Child asks.
    choices: [yes, no]
outputs:
  said: ${{ nodes.ask.choice }}
"""


def test_given_child_waiting_for_a_person_when_the_parent_is_answered_then_both_finish(
    tmp_path: Path,
) -> None:
    # Given
    root = project(
        tmp_path,
        "name: parent\nnodes:\n  call: {type: subflow, flow: child.yaml}\n"
        "outputs:\n  said: ${{ nodes.call.output.said }}\n",
    )
    (root / "child.yaml").write_text(ASKING_CHILD)

    # When
    started = cli(root, "run", "flow.yaml", "--on-wait", "exit")
    runs = root / ".arcflow" / "runs"
    parent = next(RunDir(p) for p in runs.iterdir() if not RunDir(p).meta().get("parent"))
    pending = parent.read_state()["pending_human"]
    answered = cli(root, "respond", parent.id, "--choice", "yes")

    # Then
    assert started.returncode == 4, started.stderr
    assert pending["call"]["kind"] == "child" and pending["call"]["message"] == "Child asks."
    assert answered.returncode == 0, answered.stderr
    state = wait_for_status(parent, {"succeeded", "failed"})
    assert state["status"] == "succeeded", (parent.path / "runner.log").read_text()
    assert state["outputs"] == {"said": "yes"}


def test_given_parent_running_a_child_when_cancelled_then_the_child_is_cancelled_too(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, "name: parent\nnodes:\n  call: {type: subflow, flow: child.yaml}\n")
    (root / "child.yaml").write_text("name: slow\nnodes:\n  wait: {type: shell, run: sleep 60}\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "XDG_CONFIG_HOME": str(root / "x")}
    runner = subprocess.Popen(
        [sys.executable, "-m", "arcflow", "run", "flow.yaml"],
        cwd=root,
        env=env,
        stderr=subprocess.PIPE,
    )
    import time

    deadline = time.monotonic() + 20
    runs = root / ".arcflow" / "runs"
    while time.monotonic() < deadline:
        children = [
            p
            for p in runs.glob("*")
            if (p / "run.json").exists() and json.loads((p / "run.json").read_text()).get("parent")
        ]
        if children and "attempt_started" in (children[0] / "events.jsonl").read_text():
            break
        time.sleep(0.05)
    parent_id = next(
        p.name for p in runs.iterdir() if not json.loads((p / "run.json").read_text()).get("parent")
    )

    # When
    cli(root, "cancel", parent_id)
    code = runner.wait(30)

    # Then
    assert code == 5
    statuses = sorted(RunDir(p).read_state()["status"] for p in runs.iterdir())
    assert statuses == ["cancelled", "cancelled"]


A4 = Path(__file__).parent / "flows" / "valid"


def test_given_appendix_a4_when_run_with_stubs_then_every_issue_is_triaged(tmp_path: Path) -> None:
    # Given: A.4 and its subflow, on the fake adapter, with stub gh and notify-send
    root = tmp_path
    (root / "shared").mkdir()
    (root / "schemas").mkdir()
    (root / "tools").mkdir()
    (root / "bin").mkdir()
    (root / "triage-issues.yaml").write_text((A4 / "triage-issues.yaml").read_text())
    (root / "triage-one.yaml").write_text((A4 / "triage-one.yaml").read_text())
    (root / "schemas" / "triage.json").write_text((A4 / "schemas" / "triage.json").read_text())
    (root / "shared" / "templates.yaml").write_text(
        "templates:\n  triager:\n    type: agent\n    harness: fake\n    permissions: read-only\n"
        "    harness_options:\n      responses: [{output: {label: bug, reply: Thanks!, confidence: 0.9}}]\n"
    )
    (root / "tools" / "__init__.py").write_text("")
    (root / "tools" / "triage.py").write_text(
        "def needs_triage(issues):\n    return [i for i in issues if not i['labels']]\n"
    )
    issues = [
        {"number": 1, "title": "Crash", "body": "It crashes", "labels": []},
        {"number": 2, "title": "Docs", "body": "Typo", "labels": [{"name": "docs"}]},
        {"number": 3, "title": "Slow", "body": "It is slow", "labels": []},
    ]
    gh = root / "bin" / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"if sys.argv[1:3] == ['issue', 'list']:\n    print(json.dumps({issues!r}))\n"
        "else:\n    open('gh-calls.txt', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    notify = root / "bin" / "notify-send"
    notify.write_text('#!/bin/sh\necho "$2" > notified.txt\n')
    for script in (gh, notify):
        script.chmod(0o755)
    (root / ".arcflow").mkdir()
    env = {
        "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root / "x"),
    }

    # When
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arcflow",
            "run",
            "triage-issues.yaml",
            "--input",
            "since=2026-09-01",
            "--json",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Then
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["outputs"] == {"triaged": 2, "failed": 0}
    calls = (root / "gh-calls.txt").read_text().splitlines()
    assert "issue edit 1 --add-label bug" in calls and "issue comment 3 --body Thanks!" in calls


MAP_FLOW = """name: mapper
nodes:
  each:
    type: map
    items: [1, 2]
    flow: child.yaml
    inputs: {n: "${{ item }}"}
outputs:
  count: ${{ nodes.each.succeeded }}
"""
MAP_CHILD = """name: item
inputs:
  n: {type: integer, required: true}
nodes:
  touch:
    type: shell
    run: echo "$N" >> items.txt
    env: {N: "${{ inputs.n }}"}
"""


def _cli(
    root: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root / "x"),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "arcflow", *args], cwd=root, env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "crash",
    [
        {"ARCFLOW_TEST_CRASH_AT": "child_run:1"},
        {"ARCFLOW_TEST_CRASH_AT": "child_run:2"},
        {"ARCFLOW_TEST_CRASH_CHILD_AT": "visit_started:1"},
        {"ARCFLOW_TEST_CRASH_CHILD_AT": "visit_finished:1"},
        {"ARCFLOW_TEST_CRASH_CHILD_AT": "route_taken:1"},
    ],
    ids=[
        "after-first-child-created",
        "between-items",
        "child-visit-started",
        "child-visit-finished",
        "child-routed",
    ],
)
def test_given_crash_inside_or_between_child_runs_when_resumed_then_the_map_completes(
    tmp_path: Path, crash: dict[str, str]
) -> None:
    # Given
    root = project(tmp_path, MAP_FLOW)
    (root / "child.yaml").write_text(MAP_CHILD)
    crashed = _cli(root, "run", "flow.yaml", env_extra=crash)

    # When
    runs = root / ".arcflow" / "runs"
    parent = next(RunDir(p) for p in runs.iterdir() if not RunDir(p).meta().get("parent"))
    resumed = _cli(root, "resume", parent.id)

    # Then
    assert crashed.returncode == 137, crashed.stderr
    assert resumed.returncode == 0, resumed.stderr
    assert parent.read_state()["outputs"] == {"count": 2}
    children = [e for e in read_log(parent.events).events if e["type"] == "child_run"]
    assert [c["data"]["item_index"] for c in children] == [0, 1]
    finished_children = [
        RunDir(runs / c["data"]["run_id"]).read_state()["status"] for c in children
    ]
    assert finished_children == ["succeeded", "succeeded"]
