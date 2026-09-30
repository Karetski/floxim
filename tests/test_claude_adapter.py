"""The Claude Code adapter, offline: stream parsing, command lines, and the
process path through a stub executable (spec §8.3, §8.4, §8.8)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from engine_support import Result, run_flow

from arcflow.adapters import AgentRequest, SessionSpec
from arcflow.adapters.claude import ClaudeAdapter, ClaudeStream
from arcflow.adapters.process import parse_lines, version_in_range
from arcflow.conformance import FIXTURES_DIR

FIXTURES = FIXTURES_DIR / "claude-2.1.285"
SESSION = "11111111-2222-4333-8444-555555555555"


def request(**changes: Any) -> AgentRequest:
    base: dict[str, Any] = dict(
        run_id="r",
        node_id="n",
        visit=1,
        attempt=1,
        prompt="Do it.",
        instructions=None,
        cwd="/work",
        add_dirs=[],
        model=None,
        effort=None,
        permissions="edit",
        allow_tools=[],
        deny_tools=[],
        output_schema=None,
        session=SessionSpec("new", None, SESSION),
        max_turns=None,
        budget_usd=None,
        budget_tokens=None,
        bare=False,
        env={},
        options={},
        attempt_dir="/tmp",
    )
    base.update(changes)
    return AgentRequest(**base)


def parse(name: str, req: AgentRequest | None = None) -> Any:
    req = req or request()
    parser = ClaudeStream(req)
    events = parse_lines(parser, (FIXTURES / f"{name}.jsonl").read_text().splitlines())
    return events, parser.finish(req, 0, "")


# -- parsing recorded streams ---------------------------------------------------------


def test_given_successful_stream_when_parsed_then_the_result_is_normalized() -> None:
    # When
    events, result = parse("success")

    # Then
    assert result.outcome == "succeeded"
    assert result.text == "Done: the README has a title."
    assert result.session_id == SESSION
    assert result.harness_version == "2.1.285"
    assert result.cost_usd == pytest.approx(0.0123)
    # input_tokens include cache reads and writes; cached_input_tokens are the reads
    assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (2620, 2300)
    assert result.usage.output_tokens == 65
    kinds = [type(e).__name__ for e in events]
    assert {"Text", "ToolCall", "ToolResult", "UsageUpdate"} <= set(kinds)


@pytest.mark.parametrize(
    ("name", "outcome", "kind"),
    [
        ("structured-output", "succeeded", None),
        ("schema-failure", "schema_invalid", "schema"),
        ("budget-stop", "budget_exceeded", "budget"),
        ("sigint-result", "failed", "harness_error"),
        ("sigterm-no-result", "failed", "no_result"),
        ("missing-result", "failed", "no_result"),
        ("auth-failure", "failed", "harness_error"),
    ],
)
def test_given_recorded_stream_when_parsed_then_the_outcome_follows_the_result_event(
    name: str, outcome: str, kind: str | None
) -> None:
    # When
    _, result = parse(name)

    # Then
    assert result.outcome == outcome
    assert (result.error.kind if result.error else None) == kind


def test_given_structured_output_when_parsed_then_it_is_the_output() -> None:
    _, result = parse("structured-output")
    assert result.output == {"status": "ready", "summary": "Plan"}


def test_given_permission_denials_when_parsed_then_they_are_reported() -> None:
    _, result = parse("permission-denied")
    assert [d.tool for d in result.permission_denials] == ["Bash"]


def test_given_resumed_session_when_parsed_then_cost_is_the_delta_from_the_last_total() -> None:
    # Given: the session had cost 0.0100 before this call; the harness reports 0.0123
    req = request(session=SessionSpec("resume", SESSION, None, 0.0100))

    # When
    _, result = parse("success", req)

    # Then
    assert result.cost_usd == pytest.approx(0.0023)
    assert result.session_total_usd == pytest.approx(0.0123)


# -- command lines ----------------------------------------------------------------------


def argv(**changes: Any) -> list[str]:
    return ClaudeAdapter().command(request(**changes))


def test_given_new_session_when_command_built_then_arcflow_chooses_the_id() -> None:
    line = argv()
    assert line[:2] == ["claude", "-p"]
    assert line[line.index("--session-id") + 1] == SESSION
    assert line[2:5] == ["--output-format", "stream-json", "--verbose"]
    denied = line[line.index("--disallowedTools") + 1 :]
    assert denied[0] == "Workflow"


def test_given_resume_and_fork_when_command_built_then_the_session_flags_follow() -> None:
    resume = argv(session=SessionSpec("resume", "old", None))
    assert resume[resume.index("--resume") + 1] == "old" and "--session-id" not in resume
    fork = argv(session=SessionSpec("fork", "old", "new-id"))
    assert "--fork-session" in fork and fork[fork.index("--session-id") + 1] == "new-id"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("read-only", ["--permission-mode", "dontAsk", "--permission-prompts", "none"]),
        ("edit", ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]),
        ("full", ["--permission-mode", "bypassPermissions"]),
    ],
)
def test_given_permission_profile_when_command_built_then_its_flags_are_used(
    profile: str, expected: list[str]
) -> None:
    line = argv(permissions=profile)
    start = line.index("--permission-mode")
    assert line[start : start + len(expected)] == expected
    denied = line[line.index("--disallowedTools") + 1 :]
    assert ("Edit" in denied) == (profile == "read-only")


def test_given_all_options_when_command_built_then_each_is_passed(tmp_path: Path) -> None:
    line = argv(
        bare=True,
        model="sonnet",
        effort="high",
        max_turns=5,
        budget_usd=1.5,
        output_schema={"type": "object"},
        allow_tools=["Bash(npm test:*)"],
        deny_tools=["WebFetch"],
        add_dirs=["/data"],
        instructions="Be brief.",
        attempt_dir=str(tmp_path),
    )
    pairs = {line[i]: line[i + 1] for i in range(len(line) - 1) if line[i].startswith("--")}
    assert "--bare" in line
    assert (pairs["--model"], pairs["--effort"], pairs["--max-turns"]) == ("sonnet", "high", "5")
    assert pairs["--max-budget-usd"] == "1.5000"
    assert json.loads(pairs["--json-schema"]) == {"type": "object"}
    assert pairs["--allowedTools"] == "Bash(npm test:*)"
    assert "WebFetch" in line[line.index("--disallowedTools") :]
    assert pairs["--add-dir"] == "/data"
    assert Path(pairs["--append-system-prompt-file"]).read_text() == "Be brief."


def test_given_tested_versions_when_checked_then_ranges_are_honoured() -> None:
    assert version_in_range("2.1.285", ">=2.1.285,<2.2")
    assert not version_in_range("2.2.0", ">=2.1.285,<2.2")
    assert not version_in_range("2.1.200", ">=2.1.285")


# -- the process path, through a stub `claude` ------------------------------------------

STUB = r"""#!{python}
import json, os, signal, sys, time
behavior = json.load(open("stub-behavior.json"))
record = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ)}}
with open("stub-calls.jsonl", "a") as f:
    f.write(json.dumps(record) + "\n")
lines = open(behavior["fixture"]).read().splitlines()
def on_sigint(*_):
    if behavior.get("on_sigint") == "result":
        print(open(behavior["sigint_fixture"]).read().splitlines()[-1], flush=True)
        sys.exit(1)
signal.signal(signal.SIGINT, on_sigint if behavior.get("on_sigint") == "result" else signal.SIG_IGN)
for line in lines[:-1]:
    print(line, flush=True)
time.sleep(behavior.get("sleep", 0))
last = json.loads(lines[-1])
if last.get("type") == "result" and "total_cost_usd" in behavior:
    last["total_cost_usd"] = behavior["total_cost_usd"]
print(json.dumps(last), flush=True)
"""


def claude_run(
    tmp_path: Path,
    flow_nodes: str,
    behavior: dict[str, Any],
    environ: dict[str, str] | None = None,
) -> Result:
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir()
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    (tmp_path / "stub-behavior.json").write_text(json.dumps(behavior))
    config = f"harnesses:\n  claude:\n    command: {stub}\n    grace: 1s\n"
    flow = f"name: c\ndefaults:\n  agent: {{harness: claude}}\nnodes:\n{flow_nodes}"
    return run_flow(
        tmp_path,
        flow,
        virtual=False,
        environ=environ or {"PATH": os.environ["PATH"], "HOME": "/tmp"},
        config_text=config,
        grace=1.0,
    )


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "stub-calls.jsonl").read_text().splitlines()]


def test_given_stub_claude_when_a_node_runs_then_prompt_stream_and_env_are_handled(
    tmp_path: Path,
) -> None:
    # Given
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": "/tmp",
        "ANTHROPIC_API_KEY": "sk-test",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "parent",
        "UNRELATED_SECRET": "x",
    }
    behavior = {"fixture": str(FIXTURES / "success.jsonl")}

    # When
    result = claude_run(
        tmp_path, "  ask:\n    type: agent\n    prompt: Summarize.\n", behavior, environ
    )

    # Then
    assert result.status == "succeeded", result.state["failure"]
    ask = result.state["nodes"]["ask"]
    assert ask["text"] == "Done: the README has a title."
    assert ask["harness_version"] == "2.1.285"
    call = calls(tmp_path)[0]
    assert call["stdin"] == "Summarize."
    assert call["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    assert not {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "UNRELATED_SECRET"} & set(call["env"])
    session = next(e for e in result.events if e["type"] == "session_started")["data"]["session_id"]
    assert call["argv"][call["argv"].index("--session-id") + 1] == session
    stream = result.run.attempt_dir("ask", 1, 1) / "stream.jsonl"
    assert stream.read_bytes() == (FIXTURES / "success.jsonl").read_bytes()


def test_given_slow_harness_when_timed_out_then_sigint_result_is_kept(tmp_path: Path) -> None:
    # Given
    behavior = {
        "fixture": str(FIXTURES / "success.jsonl"),
        "sleep": 30,
        "on_sigint": "result",
        "sigint_fixture": str(FIXTURES / "sigint-result.jsonl"),
    }

    # When
    result = claude_run(
        tmp_path, "  slow:\n    type: agent\n    prompt: Go.\n    timeout: 1s\n", behavior
    )

    # Then
    slow = result.state["nodes"]["slow"]
    assert slow["outcome"] == "timed_out"
    assert slow["cost_usd"] == pytest.approx(0.004)  # spend reported during the grace period
    finished = next(e for e in result.events if e["type"] == "attempt_finished")
    assert finished["data"]["stopped_by"] == "sigint"


def test_given_harness_ignoring_sigint_when_timed_out_then_sigterm_ends_it(tmp_path: Path) -> None:
    # Given
    behavior = {"fixture": str(FIXTURES / "success.jsonl"), "sleep": 30, "on_sigint": "ignore"}

    # When
    result = claude_run(
        tmp_path, "  slow:\n    type: agent\n    prompt: Go.\n    timeout: 1s\n", behavior
    )

    # Then
    slow = result.state["nodes"]["slow"]
    assert (slow["outcome"], slow["error"]["kind"]) == ("timed_out", "no_result")
    finished = next(e for e in result.events if e["type"] == "attempt_finished")
    assert finished["data"]["stopped_by"] == "sigterm"


def test_given_continued_session_when_the_harness_reports_totals_then_cost_is_per_call(
    tmp_path: Path,
) -> None:
    # Given: the stub reports a running total of 0.0123 each time
    nodes = """  work:
    type: agent
    prompt: Go.
    session: continue
    next:
      - when: visits.work < 2
        to: work
      - to: end
"""
    behavior = {"fixture": str(FIXTURES / "success.jsonl"), "total_cost_usd": 0.0123}

    # When
    result = claude_run(tmp_path, nodes, behavior)

    # Then: the second call resumed the session, so only its delta counts
    costs = [v["cost_usd"] for v in result.state["nodes"]["work"]["visits"]]
    assert costs[0] == pytest.approx(0.0123)
    assert costs[1] == pytest.approx(0.0)
    second = calls(tmp_path)[1]["argv"]
    assert "--resume" in second
