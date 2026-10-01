"""Inspection commands: status, list, logs, wait, artifacts, flows, graph."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_human_node import cli, latest, project

QUICK = """name: quick
nodes:
  plan:
    type: agent
    harness: fake
    prompt: "Plan the ${{ 'thing' }}."
    harness_options: {responses: [{text: planned, cost_usd: 0.25}]}
    next: note
  note:
    type: shell
    run: echo report > "$WHISPERWIND_ARTIFACTS_DIR/report.txt"
outputs:
  planned: ${{ nodes.plan.text }}
"""
ASK = "name: ask\nnodes:\n  ask: {type: human, message: Go?, choices: [go, stop]}\n"


def js(result: subprocess.CompletedProcess[str]) -> Any:
    return json.loads(result.stdout)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = project(tmp_path, QUICK)
    (root / "ask.yaml").write_text(ASK)
    return root


def test_given_finished_run_when_status_requested_then_details_and_totals_are_shown(
    root: Path,
) -> None:
    # Given
    assert cli(root, "run", "flow.yaml").returncode == 0

    # When
    detail = js(cli(root, "status", "@last", "--json"))["data"]
    text = cli(root, "status", "@last").stdout

    # Then
    assert detail["status"] == "succeeded"
    assert detail["flow"] == "quick"
    assert detail["totals"]["usd_spent"] == pytest.approx(0.25)
    assert detail["nodes"]["plan"] == {"outcome": "succeeded", "visit": 1, "error": None}
    assert detail["outputs"] == {"planned": "planned"}
    assert "~$0.25" in text and "succeeded" in text


def test_given_runs_when_status_has_no_argument_then_only_active_runs_are_listed(
    root: Path,
) -> None:
    # Given
    assert cli(root, "run", "flow.yaml").returncode == 0
    assert cli(root, "run", "ask.yaml", "--on-wait", "exit").returncode == 4

    # When
    active = js(cli(root, "status", "--json"))["data"]["runs"]

    # Then
    assert [(r["flow"], r["status"], r["pending"]) for r in active] == [("ask", "waiting", ["ask"])]


def test_given_several_runs_when_listed_then_newest_first_and_filters_apply(root: Path) -> None:
    # Given
    for _ in range(2):
        assert cli(root, "run", "flow.yaml").returncode == 0
    assert cli(root, "run", "ask.yaml", "--on-wait", "exit").returncode == 4

    # When
    everything = js(cli(root, "list", "--json"))["data"]["runs"]
    quick = js(cli(root, "list", "--flow", "quick", "--limit", "1", "--json"))["data"]["runs"]
    waiting = js(cli(root, "list", "--status", "waiting", "--json"))["data"]["runs"]
    recent = js(cli(root, "list", "--since", "1h", "--json"))["data"]["runs"]

    # Then
    assert [r["flow"] for r in everything] == ["ask", "quick", "quick"]
    assert len(quick) == 1 and quick[0]["run_id"] == everything[1]["run_id"]
    assert [r["flow"] for r in waiting] == ["ask"]
    assert len(recent) == 3


def test_given_run_when_logs_requested_then_events_prompts_and_streams_are_shown(
    root: Path,
) -> None:
    # Given
    assert cli(root, "run", "flow.yaml").returncode == 0

    # When
    text = cli(root, "logs", "@last").stdout
    only_note = cli(root, "logs", "@last", "--node", "note").stdout
    prompt = cli(root, "logs", "@last", "--node", "plan", "--prompt").stdout
    events = [
        json.loads(line) for line in cli(root, "logs", "@last", "--events").stdout.splitlines()
    ]
    raw = cli(root, "logs", "@last", "--raw")

    # Then
    assert "run_succeeded" in text and "plan → note via next" in text
    assert "plan#1" not in only_note and "note#1" in only_note
    assert prompt == "Plan the thing."
    assert events[0]["type"] == "run_created" and events[-1]["type"] == "run_succeeded"
    assert raw.returncode == 2  # --raw needs --node


def test_given_detached_run_when_logs_followed_then_events_stream_until_it_ends(root: Path) -> None:
    # Given
    (root / "slow.yaml").write_text(
        "name: slow\nnodes:\n  a: {type: shell, run: sleep 1, next: b}\n  b: {type: set, vars: {x: 1}}\n"
    )
    assert cli(root, "run", "slow.yaml", "--detach").returncode == 0

    # When
    followed = cli(root, "logs", "@last", "--follow", "--events")

    # Then
    types = [json.loads(line)["type"] for line in followed.stdout.splitlines()]
    assert types[-1] == "run_succeeded"
    assert "visit_started" in types


@pytest.mark.parametrize(("flow", "extra", "code"), [("flow.yaml", [], 0), ("ask.yaml", [], 4)])
def test_given_detached_run_when_waited_on_then_the_exit_code_follows_its_status(
    root: Path, flow: str, extra: list[str], code: int
) -> None:
    # Given
    assert cli(root, "run", flow, "--detach").returncode == 0

    # When
    waited = cli(root, "wait", "@last", "--json")

    # Then
    assert waited.returncode == code, waited.stderr
    assert js(waited)["data"]["status"] in ("succeeded", "waiting")


def test_given_long_run_when_wait_times_out_then_exits_8(root: Path) -> None:
    (root / "long.yaml").write_text("name: long\nnodes:\n  a: {type: shell, run: sleep 30}\n")
    assert cli(root, "run", "long.yaml", "--detach").returncode == 0
    result = cli(root, "wait", "@last", "--timeout", "1s")
    assert result.returncode == 8
    cli(root, "cancel", "@last")


def test_given_node_writing_artifacts_when_listed_then_they_are_found(root: Path) -> None:
    assert cli(root, "run", "flow.yaml").returncode == 0
    found = js(cli(root, "artifacts", "@last", "--json"))["data"]["artifacts"]
    assert [(a["node"], a["visit"], Path(a["path"]).name) for a in found] == [
        ("note", 1, "report.txt")
    ]


def test_given_flow_files_when_discovered_then_validity_is_reported(root: Path) -> None:
    # Given
    flows = root / "flows"
    flows.mkdir()
    (flows / "good.yaml").write_text(QUICK)
    (flows / "bad.yaml").write_text("name: bad\nnodes:\n  a: {type: shell, next: nowhere}\n")
    (flows / "notes.yaml").write_text("just: data\n")

    # When
    found = js(cli(root, "flows", "--json"))["data"]["flows"]

    # Then
    assert [(Path(f["file"]).name, f["name"], f["valid"]) for f in found] == [
        ("bad.yaml", None, False),
        ("good.yaml", "quick", True),
    ]


def test_given_flow_when_graph_rendered_then_json_mermaid_and_dot_agree(root: Path) -> None:
    # When
    data = js(cli(root, "graph", "flow.yaml", "--json"))["data"]
    mermaid = cli(root, "graph", "flow.yaml", "--format", "mermaid").stdout
    dot = cli(root, "graph", "flow.yaml", "--format", "dot").stdout

    # Then
    assert [n["id"] for n in data["nodes"]] == ["plan", "note"]
    assert {(e["from"], e["to"]) for e in data["edges"]} == {("plan", "note"), ("note", "end")}
    assert "plan --> note" in mermaid and "note --> end_" in mermaid
    assert '"plan" -> "note";' in dot


def test_given_unknown_run_when_inspected_then_exits_6(root: Path) -> None:
    for command in (["status", "nope"], ["logs", "nope"], ["wait", "nope"], ["artifacts", "nope"]):
        assert cli(root, *command).returncode == 6, command


def test_given_crashed_runner_when_status_shown_then_the_run_is_interrupted(root: Path) -> None:
    env = {
        **os.environ,
        "WHISPERWIND_TEST_CRASH_AT": "visit_started:1",
        "XDG_CONFIG_HOME": str(root / "x"),
    }
    subprocess.run(
        [sys.executable, "-m", "whisperwind", "run", "flow.yaml"],
        cwd=root,
        env=env,
        capture_output=True,
    )
    assert js(cli(root, "status", "@last", "--json"))["data"]["status"] == "interrupted"
    assert latest(root).read_state()["status"] == "running"
