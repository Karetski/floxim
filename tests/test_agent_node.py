"""The agent node through the fake adapter (spec §5.1, §8.1, §8.3)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from engine_support import Result, run_flow

from arcflow.adapters import Capabilities, registry
from arcflow.adapters.fake import FakeAdapter
from arcflow.config import load_config
from arcflow.engine.runner import FullPermissionsRefused, create_run
from arcflow.store.events import read_log
from arcflow.store.ids import resolve_run
from arcflow.store.rundir import RunDir
from arcflow.testing import VirtualClock
from arcflow.validate import validate


def agent_flow(nodes: str, extra: str = "") -> str:
    """A flow whose nodes (written without `type`) are all fake-harness agents."""
    typed = re.sub(r"^(  [a-z_]+:)$", r"\1\n    type: agent", nodes, flags=re.M)
    return f"name: agents\n{extra}defaults:\n  agent:\n    harness: fake\nnodes:\n{typed}"


def attempts(result: Result, node: str | None = None) -> list[dict[str, Any]]:
    return [
        e
        for e in result.events
        if e["type"] == "attempt_started" and (node is None or e["node"] == node)
    ]


def test_given_scripted_agent_when_run_then_results_and_session_are_recorded(
    tmp_path: Path,
) -> None:
    # Given
    flow = agent_flow(
        """  plan:
    prompt: "Plan ${{ inputs.feature }}"
    output_schema: {type: object}
    harness_options:
      responses:
        - output: {status: ready, steps: [a, b]}
          text: Plan ready.
          usage: {input_tokens: 1200, output_tokens: 300}
          cost_usd: 0.02
""",
        extra="inputs:\n  feature: {type: string, default: login}\n",
    )

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    plan = result.state["nodes"]["plan"]
    assert plan["output"] == {"status": "ready", "steps": ["a", "b"]}
    assert plan["text"] == "Plan ready."
    assert plan["harness"] == "fake"
    assert plan["usage"]["total_tokens"] == 1500
    assert plan["cost_usd"] == 0.02
    assert (result.run.visit_dir("plan", 1) / "prompt.md").read_text() == "Plan login"
    types = result.types()
    assert types.index("session_started") < types.index("attempt_finished")
    session = next(e for e in result.events if e["type"] == "session_started")
    assert session["data"]["session_id"] == plan["session_id"]
    assert attempts(result)[0]["data"]["adapter"] == "fake"


LOOP = """  work:
    session: {session}
    prompt: "Attempt ${{{{ visits.work }}}}"
    next:
      - when: visits.work < 3
        to: work
      - to: end
"""


@pytest.mark.parametrize(
    ("session", "modes"), [("new", ["new"] * 3), ("continue", ["new", "resume", "resume"])]
)
def test_given_session_mode_when_a_node_loops_then_sessions_are_new_or_continued(
    tmp_path: Path, session: str, modes: list[str]
) -> None:
    # When
    result = run_flow(tmp_path, agent_flow(LOOP.format(session=session)))

    # Then
    started = attempts(result)
    assert [a["data"]["session_mode"] for a in started] == modes
    ids = [v["session_id"] for v in result.state["nodes"]["work"]["visits"]]
    if session == "continue":
        assert len(set(ids)) == 1
        assert started[1]["data"]["resume_session_id"] == ids[0]
    else:
        assert len(set(ids)) == 3


def test_given_resume_and_fork_of_another_node_when_run_then_its_session_is_used(
    tmp_path: Path,
) -> None:
    # Given
    flow = agent_flow(
        """  planner:
    prompt: Plan.
    next: reviewer
  reviewer:
    prompt: Review.
    session: {resume: planner}
    next: brancher
  brancher:
    prompt: Try another way.
    session: {fork: planner}
"""
    )

    # When
    result = run_flow(tmp_path, flow)

    # Then
    nodes = result.state["nodes"]
    assert nodes["reviewer"]["session_id"] == nodes["planner"]["session_id"]
    fork = attempts(result, "brancher")[0]["data"]
    assert (fork["session_mode"], fork["resume_session_id"]) == (
        "fork",
        nodes["planner"]["session_id"],
    )
    assert nodes["brancher"]["session_id"] != nodes["planner"]["session_id"]


def test_given_resume_of_a_node_that_has_not_run_when_run_then_a_new_session_is_started(
    tmp_path: Path,
) -> None:
    # Given
    flow = agent_flow(
        """  first:
    prompt: Go.
    session: {resume: later}
    next: later
  later:
    prompt: Later.
"""
    )

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert attempts(result, "first")[0]["data"]["session_mode"] == "new"
    warning = next(e for e in result.events if e["type"] == "warning")
    assert warning["data"]["code"] == "W-SESSION-NEW"


@pytest.mark.parametrize(
    ("response", "outcome", "kind"),
    [
        (
            "{outcome: failed, error: {kind: harness_error, message: crashed}}",
            "failed",
            "harness_error",
        ),
        (
            "{outcome: budget_exceeded, error: {kind: budget, message: cap}}",
            "budget_exceeded",
            "budget",
        ),
        ("{delay: 2h}", "timed_out", "harness_error"),
    ],
)
def test_given_adapter_outcome_when_run_then_the_visit_reports_it(
    tmp_path: Path, response: str, outcome: str, kind: str
) -> None:
    # Given
    flow = agent_flow(
        f"""  work:
    prompt: Go.
    timeout: 30m
    on_error: continue
    harness_options:
      responses: [{response}]
"""
    )

    # When
    result = run_flow(tmp_path, flow)

    # Then
    work = result.state["nodes"]["work"]
    assert (work["outcome"], work["error"]["kind"]) == (outcome, kind)
    if outcome == "timed_out":
        assert result.state["totals"]["active_s"] == pytest.approx(1800, abs=1)


def test_given_continue_session_when_an_attempt_fails_then_the_retry_resumes_its_session(
    tmp_path: Path,
) -> None:
    # Given
    flow = agent_flow(
        """  work:
    prompt: Go.
    session: continue
    retry: {max_attempts: 2}
    harness_options:
      responses:
        - {match: {attempt: 1}, outcome: failed, error: {message: flaky}}
        - {match: {attempt: 2}, text: done}
"""
    )

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    _, second = attempts(result)
    first_session = next(e for e in result.events if e["type"] == "session_started")["data"][
        "session_id"
    ]
    assert (second["data"]["session_mode"], second["data"]["resume_session_id"]) == (
        "resume",
        first_session,
    )


class _Limited(FakeAdapter):
    def capabilities(self) -> Capabilities:
        return Capabilities(session_id="harness", resume=False, fork=False, tokens=True)


def test_given_adapter_without_resume_when_session_continue_then_each_visit_starts_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr(registry, "load", lambda name: _Limited())

    # When
    result = run_flow(tmp_path, agent_flow(LOOP.format(session="continue")))

    # Then
    assert [a["data"]["session_mode"] for a in attempts(result)] == ["new"] * 3
    report = validate(tmp_path / "flow.yaml", implementation_gate=False)
    assert any(p.code == "W-IGNORED-OPTION" for p in report.problems)


def _cli(
    project: Path, *args: str, crash_at: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(project), "XDG_CONFIG_HOME": str(project / "x")}
    if crash_at:
        env["ARCFLOW_TEST_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, "-m", "arcflow", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )


def test_given_crash_after_session_started_when_resumed_then_that_session_is_resumed(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / ".arcflow").mkdir()
    (tmp_path / "flow.yaml").write_text(
        agent_flow(
            "  work:\n    prompt: Do the thing.\n    harness_options:\n      responses: [{text: done}]\n"
        )
    )
    assert _cli(tmp_path, "run", "flow.yaml", crash_at="session_started:1").returncode == 137

    # When
    resumed = _cli(tmp_path, "resume", "@last", "--json")

    # Then
    assert resumed.returncode == 0, resumed.stderr
    runs = tmp_path / ".arcflow" / "runs"
    run = RunDir(runs / resolve_run(runs, "@last"))
    events = read_log(run.events).events
    first_session = next(e for e in events if e["type"] == "session_started")["data"]["session_id"]
    second = [e for e in events if e["type"] == "attempt_started"][1]["data"]
    assert (second["session_mode"], second["resume_session_id"]) == ("resume", first_session)
    assert "previous run was interrupted" in (run.visit_dir("work", 1) / "prompt.md").read_text()
    assert json.loads(resumed.stdout)["data"]["status"] == "succeeded"


def test_given_full_permissions_when_run_without_allow_full_then_it_is_refused(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / ".arcflow").mkdir()
    (tmp_path / "flow.yaml").write_text(
        agent_flow("  work:\n    prompt: Go.\n    permissions: full\n")
    )
    config, _ = load_config(tmp_path)

    # When / Then
    with pytest.raises(FullPermissionsRefused):
        create_run(
            tmp_path / "flow.yaml", {}, config=config, clock=VirtualClock(), workdir=tmp_path
        )
    create_run(
        tmp_path / "flow.yaml",
        {},
        config=config,
        clock=VirtualClock(),
        workdir=tmp_path,
        allow_full=True,
    )


def test_given_pending_built_in_harness_when_validated_then_it_is_not_implemented_yet(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / "flow.yaml").write_text(
        "name: c\nnodes:\n  a: {type: agent, harness: claude, prompt: Hi.}\n"
    )

    # When
    report = validate(tmp_path / "flow.yaml")

    # Then
    assert [p.code for p in report.errors] == ["E-NOT-IMPLEMENTED"]
    assert "claude adapter" in report.errors[0].message
