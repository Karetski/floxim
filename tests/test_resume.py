"""Resume and crash safety."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow

from floxim.clock import Clock
from floxim.config import load_config
from floxim.engine.runner import ResumeOptions, ResumeRefused, Runner
from floxim.runinfo import display_status
from floxim.store.events import read_log
from floxim.store.ids import resolve_run
from floxim.store.rundir import RunDir
from floxim.testing import VirtualClock, run_virtual

COUNTER = """
name: counter
nodes:
  count:
    type: set
    vars:
      n: ${{ default(vars.n, 0) + 1 }}
    next: check
  check:
    type: condition
    next:
      - when: vars.n < 2
        to: nap
      - to: end
  nap:
    type: sleep
    duration: 0s
    next: count
outputs:
  total: ${{ vars.n }}
"""

FIX_LOOP = """
name: fix-loop
nodes:
  implement:
    type: shell
    run: echo "$FLOXIM_NODE_ID $FLOXIM_VISIT $FLOXIM_ATTEMPT" >> side-effects.txt
    next: test
  test:
    type: shell
    run: "[ $(grep -c '^implement' side-effects.txt) -ge 2 ]"
    on_error: continue
    next:
      - when: nodes.test.exit_code == 0
        to: end
      - when: visits.implement < 4
        to: implement
      - to: fail
"""


APPROVAL = """
name: approval
nodes:
  build:
    type: shell
    run: echo "$FLOXIM_NODE_ID $FLOXIM_VISIT" >> side-effects.txt
    next: approve
  approve:
    type: human
    message: Ship it?
    choices: [ship, stop]
    next:
      - when: nodes.approve.choice == "ship"
        to: ship
      - to: end
  ship:
    type: set
    vars: {shipped: true}
outputs:
  shipped: ${{ vars.shipped }}
"""


# -- helpers -------------------------------------------------------------------------


def _cli(
    project: Path, *args: str, crash_at: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(project),
        "XDG_CONFIG_HOME": str(project / "xdg"),
    }
    if crash_at:
        env["FLOXIM_TEST_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, "-m", "floxim", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _project(tmp_path: Path, name: str, flow: str) -> Path:
    project = tmp_path / name
    (project / ".floxim").mkdir(parents=True)
    (project / "flow.yaml").write_text(flow)
    return project


def _latest(project: Path) -> RunDir:
    runs = project / ".floxim" / "runs"
    return RunDir(runs / resolve_run(runs, "@last"))


def _summary(run: RunDir) -> dict[str, Any]:
    """The parts of a run's final state that must not depend on crashes."""
    state = run.read_state()
    nodes = {
        node_id: {
            "outcome": result["outcome"],
            "visit": result["visit"],
            **{
                k: result[k]
                for k in ("exit_code", "stdout", "values", "branch", "choice")
                if k in result
            },
        }
        for node_id, result in state["nodes"].items()
    }
    return {
        "status": state["status"],
        "outputs": state["outputs"],
        "vars": state["vars"],
        "visits": state["visits"],
        "nodes": nodes,
    }


def _assert_no_finished_visit_ran_twice(run: RunDir) -> None:
    events = read_log(run.events).events
    finished = Counter(
        (e["node"], e["visit"])
        for e in events
        if e["type"] == "visit_finished" and e["data"]["outcome"] != "interrupted"
    )
    assert all(count == 1 for count in finished.values()), finished
    started = Counter((e["node"], e["visit"]) for e in events if e["type"] == "visit_started")
    assert all(count == 1 for count in started.values()), started


# -- the crash matrix ----------------------------------------------------------------


def _finish(project: Path, first: subprocess.CompletedProcess[str]) -> int:
    """Drive a run to its end: answer each prompt it stops at, then resume it."""
    code = first.returncode
    for _ in range(5):
        if code != 4:
            return code
        answered = _cli(project, "respond", "@last", "--choice", "ship", "--no-continue")
        assert answered.returncode == 0, answered.stderr
        code = _cli(project, "resume", "@last", "--on-wait", "exit").returncode
    return code


@pytest.mark.parametrize(
    ("name", "flow"), [("counter", COUNTER), ("fix-loop", FIX_LOOP), ("approval", APPROVAL)]
)
def test_given_crash_at_every_event_boundary_when_resumed_then_the_run_ends_as_if_uncrashed(
    tmp_path: Path, name: str, flow: str
) -> None:
    # Given: an uncrashed run as the reference
    reference = _project(tmp_path, "reference", flow)
    assert _finish(reference, _cli(reference, "run", "flow.yaml", "--on-wait", "exit")) in (0, 1)
    expected = _summary(_latest(reference))
    events = read_log(_latest(reference).events).events
    boundaries = []
    seen: Counter[str] = Counter()
    # run_created is written before the runner starts (see the pending-run test), and
    # crashing after the final event leaves nothing to resume.
    for event in events[1:-1]:
        if event["type"] == "human_responded":
            continue  # written by `floxim respond`, not by a runner
        seen[event["type"]] += 1
        boundaries.append(f"{event['type']}:{seen[event['type']]}")

    for index, crash_at in enumerate(boundaries):
        project = _project(tmp_path, f"crash-{index}", flow)

        # When
        crashed = _cli(project, "run", "flow.yaml", "--on-wait", "exit", crash_at=crash_at)
        if crashed.returncode == 4:  # stopped at the prompt before reaching the crash point
            _cli(project, "respond", "@last", "--choice", "ship", "--no-continue")
            crashed = _cli(project, "resume", "@last", "--on-wait", "exit", crash_at=crash_at)
        resumed = _finish(project, _cli(project, "resume", "@last", "--on-wait", "exit"))

        # Then
        assert crashed.returncode == 137, (crash_at, crashed.stderr)
        assert resumed in (0, 1), crash_at
        run = _latest(project)
        assert _summary(run) == expected, crash_at
        _assert_no_finished_visit_ran_twice(run)


def test_given_created_but_never_started_run_when_resumed_then_it_starts(tmp_path: Path) -> None:
    # Given
    from floxim.engine.runner import create_run

    project = _project(tmp_path, "pending", COUNTER)
    config, _ = load_config(project)
    run = create_run(
        project / "flow.yaml", {}, config=config, clock=VirtualClock(), workdir=project
    )

    # When
    resumed = _cli(project, "resume", run.id)

    # Then
    assert resumed.returncode == 0, resumed.stderr
    assert _summary(run)["outputs"] == {"total": 2}


# -- signals ---------------------------------------------------------------------------


def test_given_foreground_run_when_interrupted_then_it_detaches_and_resumes_later(
    tmp_path: Path,
) -> None:
    # Given
    project = _project(
        tmp_path,
        "signal",
        "name: slow\nnodes:\n  work:\n    type: shell\n    run: sleep 30\n",
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(project), "XDG_CONFIG_HOME": str(project / "x")}
    process = subprocess.Popen(
        [sys.executable, "-m", "floxim", "run", "flow.yaml"],
        cwd=project,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20
    runs = project / ".floxim" / "runs"
    while time.monotonic() < deadline:
        logs = list(runs.glob("*/events.jsonl")) if runs.exists() else []
        if logs and "attempt_started" in logs[0].read_text():
            break
        time.sleep(0.05)

    # When
    process.send_signal(signal.SIGINT)
    code = process.wait(30)

    # Then
    assert code == 8
    run = _latest(project)
    types = [e["type"] for e in read_log(run.events).events]
    assert types[-2:] == ["attempt_finished", "runner_detached"]
    assert run.read_state()["status"] == "running"
    state = run.read_state()
    assert state["in_progress"]["attempts"][-1]["outcome"] == "interrupted"
    assert display_status(run, state, Clock()) == "interrupted"


# -- resume options ------------------------------------------------------------------------

GATE = """
name: gate
nodes:
  prepare:
    type: set
    vars: {ready: true}
    next: check
  check:
    type: condition
    next:
      - when: visits.check >= 2
        to: end
      - to: fail
        reason: not yet
"""


def _resume(run: RunDir, tmp_path: Path, **options: Any) -> str:
    config, _ = load_config(tmp_path)
    runner = Runner(
        run, config, VirtualClock(), heartbeat=False, environ={}, resume=ResumeOptions(**options)
    )
    return run_virtual(runner.run()).status


def test_given_succeeded_run_when_resumed_then_it_is_refused(tmp_path: Path) -> None:
    # Given
    result = run_flow(tmp_path, "name: done\nnodes:\n  a: {type: set, vars: {x: 1}}\n")

    # When / Then
    with pytest.raises(ResumeRefused, match="succeeded"):
        _resume(result.run, tmp_path)


def test_given_failed_run_when_resumed_without_force_then_it_is_refused(tmp_path: Path) -> None:
    # Given
    result = run_flow(tmp_path, GATE)
    assert result.status == "failed"

    # When / Then
    with pytest.raises(ResumeRefused, match="--force"):
        _resume(result.run, tmp_path)


def test_given_failed_run_when_forced_then_it_continues_from_the_failed_node(
    tmp_path: Path,
) -> None:
    # Given
    result = run_flow(tmp_path, GATE)

    # When
    status = _resume(result.run, tmp_path, force=True)

    # Then
    assert status == "succeeded"
    events = read_log(result.run.events).events
    assert [e["type"] for e in events].count("run_reopened") == 1
    assert [e["node"] for e in events if e["type"] == "visit_started"] == [
        "prepare",
        "check",
        "check",
    ]


def test_given_forced_resume_from_a_node_when_run_then_earlier_visits_stay_recorded(
    tmp_path: Path,
) -> None:
    # Given
    result = run_flow(tmp_path, GATE)

    # When
    status = _resume(result.run, tmp_path, force=True, from_node="prepare")

    # Then
    assert status == "succeeded"
    assert result.run.read_state()["visits"] == {"prepare": 2, "check": 2}


def test_given_edited_flow_when_resumed_with_reload_then_the_new_snapshot_is_used(
    tmp_path: Path,
) -> None:
    # Given
    result = run_flow(tmp_path, GATE.replace("visits.check >= 2", "visits.check >= 9"))
    (tmp_path / "flow.yaml").write_text(GATE.replace("visits.check >= 2", "'true'"))

    # When
    status = _resume(result.run, tmp_path, force=True, reload=True)

    # Then
    assert status == "succeeded"
    reloaded = [e for e in read_log(result.run.events).events if e["type"] == "flow_reloaded"]
    assert reloaded[0]["data"]["snapshot"].startswith("snapshot-1/")
    assert (result.run.path / reloaded[0]["data"]["snapshot"]).exists()


def test_given_reload_removing_a_visited_node_when_resumed_then_it_is_refused(
    tmp_path: Path,
) -> None:
    # Given
    result = run_flow(tmp_path, GATE)
    (tmp_path / "flow.yaml").write_text(
        "name: gate\nnodes:\n  check:\n    type: condition\n    next: [{to: end}]\n"
    )

    # When / Then
    with pytest.raises(ResumeRefused, match="prepare"):
        _resume(result.run, tmp_path, force=True, reload=True)
    assert "flow_reloaded" not in (result.run.events).read_text()


def test_given_crash_mid_attempt_when_resumed_with_rerun_then_a_new_attempt_runs(
    tmp_path: Path,
) -> None:
    # Given
    project = _project(
        tmp_path,
        "rerun",
        "name: once\nnodes:\n  work:\n    type: shell\n    run: echo $FLOXIM_ATTEMPT >> tries\n",
    )
    assert _cli(project, "run", "flow.yaml", crash_at="attempt_started:1").returncode == 137

    # When
    resumed = _cli(project, "resume", "@last", "--rerun", "--json")

    # Then
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["data"]["status"] == "succeeded"
    attempts = [
        (e["attempt"], e["data"]["outcome"])
        for e in read_log(_latest(project).events).events
        if e["type"] == "attempt_finished"
    ]
    assert attempts == [(1, "interrupted"), (2, "succeeded")]


def test_given_torn_log_when_resumed_then_the_torn_line_is_dropped_with_a_warning(
    tmp_path: Path,
) -> None:
    # Given
    project = _project(tmp_path, "torn", COUNTER)
    assert _cli(project, "run", "flow.yaml", crash_at="route_taken:2").returncode == 137
    run = _latest(project)
    with open(run.events, "ab") as handle:
        handle.write(b'{"v":1,"seq":999,"type":"visit_sta')

    # When
    resumed = _cli(project, "resume", "@last")

    # Then
    assert resumed.returncode == 0, resumed.stderr
    warnings = [e for e in read_log(run.events).events if e["type"] == "warning"]
    assert warnings[0]["data"]["code"] == "W-TORN-LOG"


def test_given_resume_of_unknown_run_when_requested_then_exits_6(tmp_path: Path) -> None:
    # Given
    project = _project(tmp_path, "none", COUNTER)

    # When / Then
    assert _cli(project, "resume", "nope").returncode == 6
