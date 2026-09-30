"""Cancel, notify, lazy human timeouts, resume --due and on_resume: ask
(spec §5.11, §6.7, §6.11, §7.5, §7.6)."""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow
from test_human_node import cli, latest, project, wait_for_status

from floxim.store.events import read_log

ENV = {"PATH": os.environ["PATH"], "HOME": "/tmp"}


def start_run(root: Path, *args: str) -> subprocess.Popen[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "XDG_CONFIG_HOME": str(root / "x")}
    return subprocess.Popen(
        [sys.executable, "-m", "floxim", "run", "flow.yaml", *args],
        cwd=root,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_event(root: Path, kind: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = list((root / ".floxim" / "runs").glob("*/events.jsonl"))
        if logs and f'"type":"{kind}"' in logs[0].read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event")


# -- cancel ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node",
    [
        "  work:\n    type: shell\n    run: sleep 60\n",
        "  work:\n    type: agent\n    harness: fake\n    prompt: Go.\n"
        "    harness_options: {responses: [{delay: 1h}]}\n",
    ],
    ids=["shell", "agent"],
)
def test_given_running_attempt_when_cancelled_then_it_stops_and_the_run_ends_cancelled(
    tmp_path: Path, node: str
) -> None:
    # Given
    root = project(tmp_path, f"name: long\nnodes:\n{node}")
    runner = start_run(root)
    wait_for_event(root, "attempt_started")

    # When
    cancelled = cli(root, "cancel", "@last", "--reason", "enough", "--json")
    code = runner.wait(30)

    # Then
    assert code == 5
    assert json.loads(cancelled.stdout)["data"]["status"] == "cancelled"
    state = latest(root).read_state()
    assert state["status"] == "cancelled"
    assert state["nodes"]["work"]["outcome"] == "cancelled"
    types = [e["type"] for e in read_log(latest(root).events).events]
    assert types[-2:] == ["visit_finished", "run_cancelled"]
    assert "route_taken" not in types[types.index("cancel_requested") :]


def test_given_waiting_run_without_runner_when_cancelled_then_it_ends_at_once(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, "name: w\nnodes:\n  ask: {type: human, message: Go?, ack: true}\n")
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4

    # When
    result = cli(root, "cancel", "@last", "--json")

    # Then
    assert result.returncode == 0
    assert json.loads(result.stdout)["data"] == {
        "run_id": latest(root).id,
        "status": "cancelled",
        "delivered": "cancelled",
    }
    assert cli(root, "cancel", "@last").returncode == 2  # already finished


# -- notify ------------------------------------------------------------------------------


def test_given_notify_command_when_run_then_it_gets_the_message(tmp_path: Path) -> None:
    # Given
    flow = """name: n
nodes:
  tell:
    type: notify
    message: "Done: ${{ 1 + 1 }}"
    command: 'printf "%s" "$FLOXIM_MESSAGE" > told.txt'
"""

    # When
    result = run_flow(tmp_path, flow, virtual=False, environ=ENV)

    # Then
    assert result.status == "succeeded"
    assert (tmp_path / "told.txt").read_text() == "Done: 2"


@pytest.mark.parametrize(("required", "status"), [("false", "succeeded"), ("true", "failed")])
def test_given_failing_notification_when_run_then_only_a_required_one_fails(
    tmp_path: Path, required: str, status: str
) -> None:
    # Given
    flow = f"name: n\nnodes:\n  tell:\n    type: notify\n    message: hi\n    command: exit 3\n    required: {required}\n"

    # When
    result = run_flow(tmp_path, flow, virtual=False, environ=ENV)

    # Then
    assert result.status == status
    if required == "false":
        assert any(e["type"] == "warning" for e in result.events)


def test_given_webhook_when_notified_then_the_json_body_is_posted(tmp_path: Path) -> None:
    # Given
    received: list[Any] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        pytest.skip(f"cannot listen locally here: {exc}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flow = f"""name: hook
nodes:
  tell:
    type: notify
    message: "Review finished"
    webhook:
      url: http://127.0.0.1:{server.server_port}/hook
      body: {{text: "${{{{ node.message }}}}", run: "${{{{ run.flow }}}}"}}
    required: true
"""

    # When
    result = run_flow(tmp_path, flow, virtual=False, environ=ENV)
    server.shutdown()

    # Then
    assert result.status == "succeeded", result.state["failure"]
    assert received == [{"text": "Review finished", "run": "hook"}]


# -- timeouts and --due ------------------------------------------------------------------------


def test_given_passed_deadline_when_resume_due_runs_then_the_default_answer_continues_the_run(
    tmp_path: Path,
) -> None:
    # Given
    flow = """name: late
nodes:
  ask:
    type: human
    message: Go?
    choices: [go, stop]
    timeout: 1s
    default: stop
"""
    root = project(tmp_path, flow)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    time.sleep(1.2)

    # When
    due = cli(root, "resume", "--due", "--json")

    # Then
    assert json.loads(due.stdout)["data"]["resumed"] == [latest(root).id]
    state = wait_for_status(latest(root), {"succeeded", "failed"})
    assert (state["nodes"]["ask"]["choice"], state["nodes"]["ask"]["via"]) == ("stop", "timeout")


def test_given_passed_deadline_when_someone_responds_then_the_timeout_wins(tmp_path: Path) -> None:
    # Given
    flow = "name: late\nnodes:\n  ask: {type: human, message: Go?, choices: [go, stop], timeout: 1s, default: stop}\n"
    root = project(tmp_path, flow)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    time.sleep(1.2)

    # When
    answered = cli(root, "respond", "@last", "--choice", "go")

    # Then
    assert answered.returncode == 6
    state = wait_for_status(latest(root), {"succeeded", "failed"})
    assert state["nodes"]["ask"]["choice"] == "stop"


# -- on_resume: ask ------------------------------------------------------------------------------

ASK = """name: careful
nodes:
  deploy:
    type: shell
    run: echo "$FLOXIM_ATTEMPT" >> deploys.txt
    on_resume: ask
    next: after
  after:
    type: set
    vars: {done: true}
"""


@pytest.mark.parametrize(
    ("choice", "deploys", "status"),
    [("skip", 0, "succeeded"), ("rerun", 1, "succeeded"), ("fail", 0, "failed")],
)
def test_given_interrupted_visit_with_on_resume_ask_when_resumed_then_a_person_decides(
    tmp_path: Path, choice: str, deploys: int, status: str
) -> None:
    # Given: crash right after the attempt started, before the command ran
    root = project(tmp_path, ASK)
    env_crash = cli(root, "run", "flow.yaml")  # a clean run first, to prove the flow works
    assert env_crash.returncode == 0
    (root / "deploys.txt").unlink()
    crashed = subprocess.run(
        [sys.executable, "-m", "floxim", "run", "flow.yaml"],
        cwd=root,
        env={
            **os.environ,
            "FLOXIM_TEST_CRASH_AT": "attempt_started:1",
            "XDG_CONFIG_HOME": str(root / "x"),
        },
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 137

    # When
    asked = cli(root, "resume", "@last", "--on-wait", "exit")
    answered = cli(root, "respond", "@last", "--choice", choice)

    # Then
    assert asked.returncode == 4, asked.stderr
    waiting = [e for e in read_log(latest(root).events).events if e["type"] == "human_waiting"]
    assert waiting[0]["data"]["kind"] == "resume"
    assert answered.returncode == 0, answered.stderr
    state = wait_for_status(latest(root), {"succeeded", "failed"})
    assert state["status"] == status
    lines = (
        (root / "deploys.txt").read_text().splitlines() if (root / "deploys.txt").exists() else []
    )
    assert len(lines) == deploys


# -- dogfooding ----------------------------------------------------------------------------------


def test_given_the_build_milestone_flow_when_run_on_the_fake_adapter_then_it_commits(
    tmp_path: Path,
) -> None:
    # Given: Floxim's own flow, with the harnesses swapped for scripted fakes
    repo = Path(__file__).parent.parent
    flow = (repo / "flows" / "build-milestone.yaml").read_text()
    flow = flow.replace(
        "    harness: claude\n",
        "    harness: fake\n    harness_options:\n      responses:\n"
        "        - {match: {node: plan}, output: {status: ready, summary: Add the widget, steps: [test, code]}}\n"
        "        - {match: {node: implement}, text: done}\n",
    )
    flow = flow.replace(
        "    harness: codex\n",
        "    harness: fake\n    harness_options:\n      responses: [{output: {verdict: approve, issues: []}}]\n",
    )
    flow = flow.replace(
        "run: uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q",
        "run: echo checks pass",
    )
    flow = flow.replace(
        'git commit -m "$MILESTONE: $SUMMARY"', 'echo "$MILESTONE: $SUMMARY" > committed.txt'
    )
    root = project(tmp_path, flow)
    (root / "prompts").mkdir()
    (root / "schemas").mkdir()
    for name in ("plan-milestone.md", "implement-milestone.md", "review-milestone.md"):
        (root / "prompts" / name).write_text((repo / "flows" / "prompts" / name).read_text())
    for name in ("milestone-plan.json", "review.json"):
        (root / "schemas" / name).write_text((repo / "flows" / "schemas" / name).read_text())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    # When
    assert re.search(r"harness: fake", flow)
    started = cli(root, "run", "flow.yaml", "--on-wait", "exit", "--input", "milestone=M9z")
    answered = cli(root, "respond", "@last", "approve", "--choice", "commit")

    # Then
    assert started.returncode == 4, started.stderr
    assert answered.returncode == 0, answered.stderr
    state = wait_for_status(latest(root), {"succeeded", "failed"})
    assert state["status"] == "succeeded", (latest(root).path / "runner.log").read_text()
    assert (root / "committed.txt").read_text() == "M9z: Add the widget\n"
