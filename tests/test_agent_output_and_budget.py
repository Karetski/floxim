"""Structured agent output and budgets (spec §5.1.2, §6.8)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from engine_support import Result, run_flow

from floxim.adapters import Capabilities, registry
from floxim.adapters.fake import FakeAdapter
from floxim.engine.structured import extract_json
from floxim.validate import validate

SCHEMA = "{type: object, required: [status], properties: {status: {enum: [ready, blocked]}}}"


def flow(nodes: str, extra: str = "") -> str:
    typed = re.sub(r"^(  [a-z_]+:)$", r"\1\n    type: agent\n    harness: fake", nodes, flags=re.M)
    return f"name: out\n{extra}nodes:\n{typed}"


def events(result: Result, kind: str) -> list[dict[str, Any]]:
    return [e for e in result.events if e["type"] == kind]


def test_given_invalid_output_when_the_fix_is_valid_then_the_session_is_resumed_with_errors(
    tmp_path: Path,
) -> None:
    # Given
    nodes = f"""  plan:
    prompt: Plan.
    output_schema: {SCHEMA}
    harness_options:
      responses:
        - {{output: {{status: maybe}}, usage: {{input_tokens: 100, output_tokens: 10}}, cost_usd: 0.01}}
        - {{output: {{status: ready}}, usage: {{input_tokens: 50, output_tokens: 5}}, cost_usd: 0.005}}
"""

    # When
    result = run_flow(tmp_path, flow(nodes))

    # Then
    assert result.status == "succeeded"
    plan = result.state["nodes"]["plan"]
    assert plan["output"] == {"status": "ready"}
    assert plan["usage"]["total_tokens"] == 165
    assert plan["cost_usd"] == pytest.approx(0.015)
    assert plan["attempts"] == 1
    retry = events(result, "schema_retry")
    assert len(retry) == 1 and "status" in retry[0]["data"]["errors"][0]
    sessions = [e["data"]["session_id"] for e in events(result, "session_started")]
    assert len(sessions) == 2 and sessions[0] == sessions[1]  # the fix resumed the same session
    assert result.state["totals"]["usd_spent"] == pytest.approx(0.015)


def test_given_output_that_stays_invalid_when_fixes_run_out_then_schema_invalid(
    tmp_path: Path,
) -> None:
    # Given
    nodes = f"""  plan:
    prompt: Plan.
    output_schema: {SCHEMA}
    schema_retries: 1
    retry: {{max_attempts: 3}}
    on_error: continue
    harness_options:
      responses: [{{output: {{status: maybe}}}}, {{output: {{nope: 1}}}}]
"""

    # When
    result = run_flow(tmp_path, flow(nodes))

    # Then
    plan = result.state["nodes"]["plan"]
    assert plan["outcome"] == "schema_invalid"
    assert plan["schema_errors"] and plan["error"]["kind"] == "schema"
    assert plan["attempts"] == 1  # schema_invalid is never retried by `retry`


class _NoNativeOutput(FakeAdapter):
    def capabilities(self) -> Capabilities:
        return Capabilities(session_id="harness", resume=True, tokens=True)


def test_given_adapter_without_native_output_when_run_then_json_is_asked_for_and_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr(registry, "load", lambda name, root=None: _NoNativeOutput())
    nodes = f"""  plan:
    prompt: Plan.
    output_schema: {SCHEMA}
    harness_options:
      responses:
        - text: "Done.\\n```json\\n{{\\"status\\": \\"blocked\\"}}\\n```"
"""

    # When
    result = run_flow(tmp_path, flow(nodes))

    # Then
    assert result.state["nodes"]["plan"]["output"] == {"status": "blocked"}
    prompt = (result.run.visit_dir("plan", 1) / "prompt.md").read_text()
    assert prompt.startswith("Plan.") and "JSON Schema" in prompt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('prefix {"a": 1} then {"b": {"c": 2}} end', {"b": {"c": 2}}),
        ('```json\n{"x": true}\n```\nand {"y": 1}', {"x": True}),
        ("no json here", None),
        ('broken {"a": } then {"ok": 1}', {"ok": 1}),
    ],
)
def test_given_agent_text_when_json_extracted_then_the_last_object_or_fenced_block_wins(
    text: str, expected: Any
) -> None:
    assert extract_json(text) == expected


def test_given_node_budget_when_streamed_spend_crosses_it_then_the_attempt_stops(
    tmp_path: Path,
) -> None:
    # Given
    nodes = """  work:
    prompt: Work.
    budget: {usd: 0.05}
    on_error: continue
    harness_options:
      responses: [{delay: 10m, cost_usd: 0.2, usage: {input_tokens: 1000, output_tokens: 100}}]
"""

    # When
    result = run_flow(tmp_path, flow(nodes))

    # Then
    work = result.state["nodes"]["work"]
    assert work["outcome"] == "budget_exceeded"
    assert result.state["totals"]["active_s"] < 1  # stopped at once, not after the delay


def test_given_run_budget_when_spent_then_the_next_agent_visit_fails_the_run(
    tmp_path: Path,
) -> None:
    # Given
    nodes = """  first:
    prompt: One.
    harness_options: {responses: [{cost_usd: 0.03}]}
    next: second
  second:
    prompt: Two.
    harness_options: {responses: [{cost_usd: 0.03}]}
    next: third
  third:
    prompt: Three.
"""

    # When
    result = run_flow(tmp_path, flow(nodes, extra="limits:\n  budget: {usd: 0.05}\n"))

    # Then
    assert result.status == "failed"
    assert result.state["failure"]["reason"] == "budget_exceeded"
    assert result.state["failure"]["node"] == "third"
    updates = events(result, "budget_updated")
    assert updates[-1]["data"]["usd_left"] == pytest.approx(-0.01)


class _NoCost(FakeAdapter):
    def capabilities(self) -> Capabilities:
        return Capabilities(structured_output=True, session_id="caller", resume=True, tokens=True)


def test_given_adapter_without_cost_when_prices_configured_then_usd_is_estimated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr(registry, "load", lambda name, root=None: _NoCost())
    nodes = """  work:
    prompt: Work.
    model: m1
    harness_options:
      responses: [{usage: {input_tokens: 2000000, cached_input_tokens: 1000000, output_tokens: 100000}}]
"""
    config = "prices:\n  fake:\n    m1: {input_per_mtok: 1.0, cached_input_per_mtok: 0.1, output_per_mtok: 10.0}\n"

    # When
    result = run_flow(tmp_path, flow(nodes), config_text=config)

    # Then: 1M uncached × $1 + 1M cached × $0.10 + 0.1M output × $10
    assert result.state["totals"]["usd_spent"] == pytest.approx(2.1)


def test_given_usd_budget_on_adapter_without_cost_or_price_when_validated_then_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setattr(registry, "load", lambda name, root=None: _NoCost())
    path = tmp_path / "flow.yaml"
    path.write_text(flow("  work:\n    prompt: W.\n    budget: {usd: 1}\n"))

    # When
    report = validate(path, implementation_gate=False)

    # Then
    assert [p.code for p in report.problems] == ["W-USD-UNENFORCEABLE"]


IMPLEMENT_FEATURE = """
floxim: 1
name: implement-feature
description: Appendix A.1 without its human nodes, on the fake adapter.
inputs:
  feature: {type: string, required: true}
defaults:
  max_visits: 5
  agent:
    harness: fake
    timeout: 30m
limits:
  budget: {usd: 10}
nodes:
  plan:
    type: agent
    permissions: read-only
    prompt: |
      Write an implementation plan for: ${{ inputs.feature }}
    output_schema: schemas/plan.json
    harness_options:
      responses:
        - output: {status: ready, summary: Add a login form, steps: [form, tests]}
          cost_usd: 0.05
    next:
      - when: nodes.plan.output.status == "ready"
        to: implement
      - to: fail
        reason: "Planner is blocked: ${{ nodes.plan.output.summary }}"
  implement:
    type: agent
    session: continue
    prompt: |
      ${{ "Implement this plan:" if visits.implement == 1 else "The tests still fail." }}
      ${{ nodes.plan.output if visits.implement == 1 else tail(nodes.test.stdout, 5) }}
    harness_options:
      responses:
        - {match: {visit: 1}, text: first try, cost_usd: 0.1}
        - {match: {visit: 2}, text: fixed it, cost_usd: 0.1}
    next: test
  test:
    type: shell
    run: |
      echo run >> test-runs.txt
      [ $(wc -l < test-runs.txt) -ge 2 ]
    on_error: continue
    next:
      - when: nodes.test.exit_code == 0
        to: end
      - when: visits.implement < 3
        to: implement
      - to: fail
        reason: Gave up after repeated test failures.
outputs:
  summary: ${{ nodes.plan.output.summary }}
  spent: ${{ run.budget.usd_spent }}
"""


def test_given_plan_implement_test_loop_when_run_then_event_log_matches_golden(
    tmp_path: Path, golden: Any
) -> None:
    # Given
    plan_schema = (Path(__file__).parent / "flows" / "valid" / "schemas" / "plan.json").read_text()

    # When
    result = run_flow(
        tmp_path,
        IMPLEMENT_FEATURE,
        inputs={"feature": "login"},
        files={"schemas/plan.json": plan_schema},
        virtual=False,
        environ={"PATH": "/usr/bin:/bin"},
    )

    # Then
    assert result.status == "succeeded", result.state["failure"]
    assert result.state["outputs"] == {"summary": "Add a login form", "spent": 0.25}
    assert result.visited() == ["plan", "implement", "test", "implement", "test"]
    golden.check("runs/implement-feature.json", result.normalized(tmp_path, real_time=True))
