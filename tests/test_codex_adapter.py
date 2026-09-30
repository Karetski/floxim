"""The Codex adapter, offline: stream parsing, command lines, the process path
through a stub executable, and the strict-schema lint (spec §8.3, §8.4, §5.1.2)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow
from test_claude_adapter import request

from arcflow.adapters import AgentRequest, SessionSpec
from arcflow.adapters.codex import CodexAdapter, CodexStream
from arcflow.adapters.process import parse_lines
from arcflow.checks import strict_schema_problem
from arcflow.conformance import FIXTURES_DIR
from arcflow.validate import validate

FIXTURES = FIXTURES_DIR / "codex-0.147.0"
THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def parse(name: str, req: AgentRequest | None = None) -> Any:
    req = req or request(session=SessionSpec("new", None, None))
    parser = CodexStream(req, "0.147.0")
    events = parse_lines(parser, (FIXTURES / f"{name}.jsonl").read_text().splitlines())
    return events, parser.finish(req, 0, "")


def test_given_successful_stream_when_parsed_then_thread_usage_and_text_are_normalized() -> None:
    # When
    events, result = parse("success")

    # Then
    assert result.outcome == "succeeded"
    assert result.session_id == THREAD
    assert result.text == "Fixed the failing test."
    assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (24763, 24448)
    assert result.usage.reasoning_tokens == 64
    assert result.cost_usd is None  # Codex reports no USD
    assert type(events[0]).__name__ == "SessionStarted"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("turn-failed", "harness_error"),
        ("error-event", "harness_error"),
        ("missing-result", "no_result"),
    ],
)
def test_given_failed_stream_when_parsed_then_the_outcome_is_failed(name: str, kind: str) -> None:
    _, result = parse(name)
    assert (result.outcome, result.error.kind if result.error else None) == ("failed", kind)


def test_given_schema_when_parsed_then_the_last_message_is_the_output() -> None:
    req = request(session=SessionSpec("new", None, None), output_schema={"type": "object"})
    _, result = parse("structured-output", req)
    assert result.output == {"status": "fixed", "summary": "Updated the import"}


def argv(**changes: Any) -> list[str]:
    changes.setdefault("session", SessionSpec("new", None, None))
    return CodexAdapter().command(request(**changes))


@pytest.mark.parametrize(
    ("profile", "sandbox"),
    [("read-only", "read-only"), ("edit", "workspace-write"), ("full", "danger-full-access")],
)
def test_given_permission_profile_when_command_built_then_sandbox_and_approval_are_explicit(
    profile: str, sandbox: str
) -> None:
    line = argv(permissions=profile)
    assert line[:2] == ["codex", "exec"] and line[-1] == "-"
    configs = [line[i + 1] for i, a in enumerate(line) if a == "-c"]
    assert f'sandbox_mode="{sandbox}"' in configs
    assert 'approval_policy="never"' in configs
    assert "--ignore-user-config" in line


def test_given_resume_when_command_built_then_the_thread_is_resumed_without_exec_only_flags() -> (
    None
):
    line = argv(session=SessionSpec("resume", THREAD, None))
    assert line[2:4] == ["resume", THREAD]
    assert "-C" not in line and "--sandbox" not in line


def test_given_options_when_command_built_then_each_is_passed(tmp_path: Path) -> None:
    line = argv(
        model="gpt-5-codex",
        effort="high",
        add_dirs=["/data"],
        output_schema={"type": "object"},
        attempt_dir=str(tmp_path),
        options={"network": True, "profile": "ci"},
    )
    configs = [line[i + 1] for i, a in enumerate(line) if a == "-c"]
    assert "sandbox_workspace_write.network_access=true" in configs
    assert 'sandbox_workspace_write.writable_roots=["/data"]' in configs
    assert 'model_reasoning_effort="high"' in configs
    assert line[line.index("-m") + 1] == "gpt-5-codex"
    assert line[line.index("-p") + 1] == "ci"
    assert json.loads(Path(line[line.index("--output-schema") + 1]).read_text()) == {
        "type": "object"
    }


def test_given_instructions_when_prompt_sent_then_they_are_prepended_under_a_heading() -> None:
    text = CodexAdapter().stdin(request(instructions="Be careful.", prompt="Fix it."))
    assert text == "# Instructions\n\nBe careful.\n\n# Task\n\nFix it."


STUB = r"""#!{python}
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.147.0"); sys.exit(0)
with open("stub-calls.jsonl", "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ)}}) + "\n")
print(open("{fixture}").read(), end="", flush=True)
"""


def test_given_stub_codex_when_nodes_run_then_the_thread_is_captured_and_resumed(
    tmp_path: Path,
) -> None:
    # Given
    stub = tmp_path / "bin" / "codex"
    stub.parent.mkdir()
    stub.write_text(STUB.format(python=sys.executable, fixture=FIXTURES / "success.jsonl"))
    stub.chmod(0o755)
    flow = """name: cx
defaults:
  agent: {harness: codex}
nodes:
  fix:
    type: agent
    prompt: Fix the test.
    next: again
  again:
    type: agent
    prompt: Check again.
    session: {resume: fix}
"""
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": "/tmp",
        "OPENAI_API_KEY": "sk",
        "CODEX_THREAD_ID": "parent",
    }

    # When
    result = run_flow(
        tmp_path,
        flow,
        virtual=False,
        environ=environ,
        config_text=f"harnesses:\n  codex:\n    command: {stub}\n",
    )

    # Then
    assert result.status == "succeeded", result.state["failure"]
    fix = result.state["nodes"]["fix"]
    assert (fix["session_id"], fix["harness_version"]) == (THREAD, "0.147.0")
    calls = [json.loads(line) for line in (tmp_path / "stub-calls.jsonl").read_text().splitlines()]
    assert calls[0]["stdin"] == "Fix the test."
    assert calls[1]["argv"][1:3] == ["resume", THREAD]
    assert calls[0]["env"]["OPENAI_API_KEY"] == "sk" and "CODEX_THREAD_ID" not in calls[0]["env"]


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            "additionalProperties",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {}, "b": {}},
                "required": ["a"],
            },
            "b",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {"type": "array", "items": {"type": "object", "properties": {"x": {}}}}
                },
            },
            "items[]",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["a"],
                "properties": {"a": {}},
            },
            None,
        ),
    ],
)
def test_given_schema_when_checked_for_codex_strict_mode_then_loose_objects_are_named(
    schema: dict[str, Any], problem: str | None
) -> None:
    found = strict_schema_problem(schema)
    assert (found is None) if problem is None else (problem in str(found))


def test_given_codex_node_with_loose_schema_when_validated_then_warns(tmp_path: Path) -> None:
    (tmp_path / "flow.yaml").write_text(
        "name: s\nnodes:\n  a:\n    type: agent\n    harness: codex\n    prompt: Hi.\n"
        "    output_schema: {type: object, properties: {x: {type: string}}}\n"
    )
    report = validate(tmp_path / "flow.yaml", implementation_gate=False)
    assert [p.code for p in report.problems] == ["W-CODEX-STRICT-SCHEMA"]
