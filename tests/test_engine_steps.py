"""The step loop with condition, sleep and set nodes."""

from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow

COUNTER = """
name: counter
inputs:
  rounds: {type: integer, default: 3}
nodes:
  count:
    type: set
    vars:
      n: ${{ default(vars.n, 0) + 1 }}
    next: check
  check:
    type: condition
    next:
      - when: vars.n < inputs.rounds
        to: nap
      - to: end
        reason: "done after ${{ vars.n }}"
  nap:
    type: sleep
    duration: 10m
    next: count
outputs:
  total: ${{ vars.n }}
  last_branch: ${{ nodes.check.branch }}
"""


def test_given_counter_loop_when_run_then_event_log_matches_golden(
    tmp_path: Path, golden: Any
) -> None:
    # When
    result = run_flow(tmp_path, COUNTER)

    # Then
    assert result.status == "succeeded"
    assert result.state["outputs"] == {"total": 3, "last_branch": "end"}
    golden.check("runs/counter.json", result.normalized(tmp_path))


def test_given_sleeps_when_run_on_virtual_time_then_they_take_their_full_duration(
    tmp_path: Path,
) -> None:
    # When
    result = run_flow(tmp_path, COUNTER)

    # Then: two ten-minute naps
    assert result.state["totals"]["active_s"] == pytest.approx(1200, abs=1)


def test_given_unguarded_loop_when_run_then_it_stops_at_max_visits_naming_the_node(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: spin
defaults: {max_visits: 4}
nodes:
  a: {type: sleep, duration: 1s, next: b}
  b:
    type: condition
    next:
      - when: "false"
        to: end
      - to: a
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "failed"
    assert result.state["failure"]["reason"] == "max_visits_exceeded"
    assert result.state["failure"]["node"] == "a"
    assert result.visited().count("a") == 4


def test_given_max_steps_when_exceeded_then_the_run_fails(tmp_path: Path) -> None:
    # Given
    flow = """
name: steps
limits: {max_steps: 5}
nodes:
  a:
    type: condition
    next:
      - when: visits.a < 100
        to: a
      - to: end
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.state["failure"]["reason"] == "max_steps_exceeded"
    assert len(result.visited()) == 5


def test_given_max_duration_when_sleeps_would_exceed_it_then_the_run_fails(tmp_path: Path) -> None:
    # Given
    flow = """
name: slow
limits: {max_duration: 25m}
nodes:
  nap:
    type: sleep
    duration: 10m
    next:
      - when: visits.nap < 5
        to: nap
      - to: end
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then: the third nap would pass 25 minutes, so it fails, and on_error fails the run
    assert result.status == "failed"
    assert result.state["nodes"]["nap"]["error"]["kind"] == "limit"
    assert result.state["failure"]["reason"] == "node_error"


def test_given_route_to_fail_when_taken_then_its_reason_is_the_failure_message(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: gate
inputs:
  ok: {type: boolean, default: false}
nodes:
  check:
    type: condition
    next:
      - when: inputs.ok
        to: end
      - to: fail
        reason: "not ok: ${{ inputs.ok }}"
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "failed"
    assert result.state["failure"] == {
        "reason": "route_fail",
        "node": "check",
        "message": "not ok: false",
    }


@pytest.mark.parametrize(
    ("when", "reason"),
    [("inputs.n", "expression_error"), ("inputs.n > 5", "no_route")],
)
def test_given_routing_that_cannot_decide_when_run_then_the_run_fails_with_reason(
    tmp_path: Path, when: str, reason: str
) -> None:
    # Given
    flow = f"""
name: undecided
inputs:
  n: {{type: integer, default: 1}}
nodes:
  a:
    type: set
    vars: {{x: 1}}
    next:
      - when: {when}
        to: end
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "failed"
    assert result.state["failure"]["reason"] == reason


def test_given_failing_node_when_on_error_names_a_node_then_the_run_goes_there(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: recover
nodes:
  broken:
    type: set
    vars:
      x: ${{ 1 / 0 }}
    on_error: handle
  handle:
    type: set
    vars:
      seen: ${{ nodes.broken.error.kind }}
outputs:
  seen: ${{ vars.seen }}
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    assert result.state["outputs"] == {"seen": "expression_error"}
    route = next(e for e in result.events if e["type"] == "route_taken")
    assert route["data"]["via"] == "on_error"


def test_given_on_error_continue_when_node_fails_then_next_can_branch_on_outcome(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: continue
nodes:
  try_it:
    type: set
    vars: {x: "${{ 'a' + 1 }}"}
    on_error: continue
    next:
      - when: nodes.try_it.outcome == "failed"
        to: end
        reason: failure was data
      - to: fail
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "succeeded"


def test_given_on_error_cases_without_default_when_none_match_then_the_run_fails(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: cases
nodes:
  broken:
    type: set
    vars: {x: "${{ 1 / 0 }}"}
    on_error:
      - when: "false"
        to: end
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.state["failure"]["reason"] == "node_error"


def test_given_timeout_and_retries_when_attempts_time_out_then_each_retry_is_recorded(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: retry
nodes:
  nap:
    type: sleep
    duration: 1h
    timeout: 1m
    retry: {max_attempts: 3, backoff: 30s}
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    attempts = [e for e in result.events if e["type"] == "attempt_finished"]
    assert [e["data"]["outcome"] for e in attempts] == ["timed_out"] * 3
    assert result.state["nodes"]["nap"]["attempts"] == 3
    # three one-minute attempts plus backoffs of 30s and 60s
    assert result.state["totals"]["active_s"] == pytest.approx(270, abs=1)
    assert result.state["failure"]["reason"] == "node_error"


def test_given_outputs_with_error_when_run_reaches_end_then_the_run_fails(tmp_path: Path) -> None:
    # Given
    flow = """
name: bad-outputs
nodes:
  a: {type: set, vars: {x: 1}}
outputs:
  y: ${{ vars.x + "a" }}
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.state["failure"]["reason"] == "expression_error"


def test_given_visit_when_finished_then_state_exposes_common_result_fields(tmp_path: Path) -> None:
    # When
    result = run_flow(tmp_path, COUNTER)

    # Then
    count = result.state["nodes"]["count"]
    assert count["outcome"] == "succeeded"
    assert count["visit"] == 3
    assert [v["values"] for v in count["visits"]] == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert set(count) >= {"attempts", "started_at", "finished_at", "duration_s", "error"}
    assert result.state["visits"] == {"count": 3, "check": 3, "nap": 2}
    assert (result.run.path / "nodes" / "count" / "1" / "visit.json").exists()


def test_given_node_reading_its_own_visit_count_when_run_then_it_includes_the_current_visit(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: own-visits
nodes:
  loop:
    type: condition
    next:
      - when: visits.loop == 3
        to: end
      - to: loop
"""

    # When
    result = run_flow(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    assert result.visited() == ["loop", "loop", "loop"]
