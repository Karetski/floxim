"""Command adapters declared in YAML, and the conformance kit."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from engine_support import run_flow

from whisperwind import cli
from whisperwind.adapters.command import substitute
from whisperwind.validate import validate

HARNESS = """name: echo
command: ["{cli}", run, -m, "{{model}}", --cwd, "{{cwd}}"]
prompt_via: stdin
resume_command: ["{cli}", resume, "{{session_id}}"]
capabilities: {{resume: true, tokens: true, cost_usd: true}}
stream:
  format: jsonl
  session_id: {{when: 'event.type == "init"', value: event.session_id}}
  text: {{when: 'event.type == "message" and event.role == "assistant"', value: event.content}}
  usage:
    when: 'event.type == "result"'
    input_tokens: event.stats.input_tokens
    output_tokens: event.stats.output_tokens
    cost_usd: event.stats.cost
  result: {{when: 'event.type == "result"', success: 'event.status == "success"', text: event.response}}
  error: {{when: 'event.type == "error"', message: event.message}}
permissions:
  read-only: [--approval-mode, plan]
  edit: [--approval-mode, auto_edit]
env: [ECHO_API_KEY]
"""

CLI = r"""#!{python}
import json, os, sys
with open(os.path.join(os.getcwd(), "cli-calls.jsonl"), "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ)}}) + "\n")
session = sys.argv[2] if sys.argv[1] == "resume" else "sess-1"
print(json.dumps({{"type": "init", "session_id": session}}))
print(json.dumps({{"type": "message", "role": "assistant", "content": "hello"}}))
print(json.dumps({{"type": "result", "status": "success", "response": "hello", "stats": {{"input_tokens": 10, "output_tokens": 2, "cost": 0.001}}}}))
"""

FIXTURE = [
    {"type": "init", "session_id": "sess-9"},
    {"type": "message", "role": "assistant", "content": "done"},
    {
        "type": "result",
        "status": "success",
        "response": "done",
        "stats": {"input_tokens": 5, "output_tokens": 1, "cost": 0.0005},
    },
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    cli_path = tmp_path / "bin" / "echo-cli"
    cli_path.parent.mkdir()
    cli_path.write_text(CLI.format(python=sys.executable))
    cli_path.chmod(0o755)
    harnesses = tmp_path / ".whisperwind" / "harnesses"
    (harnesses / "echo" / "fixtures").mkdir(parents=True)
    (harnesses / "echo.yaml").write_text(HARNESS.format(cli=cli_path))
    fixtures = harnesses / "echo" / "fixtures"
    (fixtures / "success.jsonl").write_text("".join(json.dumps(e) + "\n" for e in FIXTURE))
    (fixtures / "no-result.jsonl").write_text(json.dumps(FIXTURE[0]) + "\n")
    (fixtures / "expected.yaml").write_text(
        "cases:\n  success: {outcome: succeeded, session_id: sess-9, text: done, cost_usd: 0.0005}\n"
        "  no-result: {outcome: failed, error_kind: no_result}\n"
    )
    return tmp_path


def test_given_command_adapter_when_a_flow_runs_then_its_stream_is_mapped(project: Path) -> None:
    # Given
    flow = """name: echo-flow
defaults:
  agent: {harness: echo}
nodes:
  first:
    type: agent
    prompt: Say hello.
    next: again
  again:
    type: agent
    permissions: read-only
    session: {resume: first}
    prompt: Again.
"""
    environ = {"PATH": os.environ["PATH"], "ECHO_API_KEY": "k", "OTHER": "x"}

    # When
    result = run_flow(project, flow, virtual=False, environ=environ)

    # Then
    assert result.status == "succeeded", result.state["failure"]
    first = result.state["nodes"]["first"]
    assert (first["text"], first["session_id"], first["cost_usd"]) == ("hello", "sess-1", 0.001)
    calls = [json.loads(line) for line in (project / "cli-calls.jsonl").read_text().splitlines()]
    assert calls[0]["argv"] == ["run", "--cwd", str(project), "--approval-mode", "auto_edit"]
    assert calls[0]["stdin"] == "Say hello."
    assert calls[0]["env"]["ECHO_API_KEY"] == "k" and "OTHER" not in calls[0]["env"]
    assert calls[1]["argv"] == ["resume", "sess-1", "--approval-mode", "plan"]


def test_given_unset_placeholder_when_substituted_then_it_drops_with_its_flag() -> None:
    template = ["cli", "-p", "{prompt}", "-m", "{model}", "--dir={cwd}", "{attempt_dir}"]
    values: dict[str, str | None] = {
        "prompt": "hi",
        "model": None,
        "cwd": "/w",
        "attempt_dir": "/a",
    }
    assert substitute(template, values) == ["cli", "-p", "hi", "--dir=/w", "/a"]


def test_given_command_adapter_fixtures_when_kit_runs_then_every_case_passes(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    monkeypatch.chdir(project)

    # When
    code = cli.main(["adapter", "test", "echo", "--json"])

    # Then
    document = json.loads(capsys.readouterr().out)
    assert code == 0, document
    assert [c["case"] for c in document["data"]["cases"]] == [
        "fixtures/success",
        "fixtures/no-result",
    ]


def test_given_wrong_expectation_when_kit_runs_then_the_case_fails(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    monkeypatch.chdir(project)
    expected = project / ".whisperwind" / "harnesses" / "echo" / "fixtures" / "expected.yaml"
    expected.write_text("cases:\n  success: {outcome: succeeded, text: nope}\n")

    # When
    code = cli.main(["adapter", "test", "echo"])

    # Then
    assert code == 1
    assert "text: expected 'nope', got 'done'" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_given_built_in_adapter_when_kit_runs_offline_then_its_fixtures_pass(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["adapter", "test", name]) == 0


def test_given_malformed_harness_file_when_a_flow_is_validated_then_its_problems_are_shown(
    tmp_path: Path,
) -> None:
    # Given
    harnesses = tmp_path / ".whisperwind" / "harnesses"
    harnesses.mkdir(parents=True)
    (harnesses / "bad.yaml").write_text(
        "name: bad\ncommand: [bad, '{prompt_text}']\nstream:\n  result: {when: 'event.type ==', success: 'true'}\n"
    )
    (tmp_path / "flow.yaml").write_text(
        "name: f\nnodes:\n  a: {type: agent, harness: bad, prompt: Hi.}\n"
    )

    # When
    report = validate(tmp_path / "flow.yaml", implementation_gate=False)

    # Then
    found = [(p.code, Path(p.file or "").name, p.line) for p in report.errors]
    assert ("E-SCHEMA", "bad.yaml", 2) in found
    assert ("E-EXPR-SYNTAX", "bad.yaml", 4) in found


def test_given_aap_harness_when_validated_then_it_is_not_implemented(tmp_path: Path) -> None:
    # Given
    harnesses = tmp_path / ".whisperwind" / "harnesses"
    harnesses.mkdir(parents=True)
    (harnesses / "ext.yaml").write_text("name: ext\nprotocol: aap\ncommand: [ext]\nstream: {}\n")
    (tmp_path / "flow.yaml").write_text(
        "name: f\nnodes:\n  a: {type: agent, harness: ext, prompt: Hi.}\n"
    )

    # When
    report = validate(tmp_path / "flow.yaml")

    # Then
    assert [p.code for p in report.errors] == ["E-NOT-IMPLEMENTED"]
