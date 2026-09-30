"""The shell node and process control (spec §5.2, §6.6, §6.7, §12.3, §12.4)."""

import json
import time
from pathlib import Path

import pytest
from engine_support import Result, run_flow

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/tmp"}


def shell(tmp_path: Path, flow: str, **kwargs: object) -> Result:
    return run_flow(tmp_path, flow, virtual=False, environ=kwargs.pop("environ", ENV), **kwargs)  # type: ignore[arg-type]


def test_given_command_when_it_succeeds_then_result_fields_hold_its_output(tmp_path: Path) -> None:
    # Given
    flow = """
name: hello
nodes:
  hi:
    type: shell
    run: |
      echo "hello $WHO"
      echo oops >&2
    env:
      WHO: world
"""

    # When
    result = shell(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    hi = result.state["nodes"]["hi"]
    assert (hi["exit_code"], hi["stdout"], hi["stderr"]) == (0, "hello world\n", "oops\n")
    assert Path(hi["stdout_file"]).read_text() == "hello world\n"
    assert hi["stdout_truncated"] is False


def test_given_failing_step_in_a_script_when_run_then_bash_errexit_and_pipefail_apply(
    tmp_path: Path,
) -> None:
    # Given
    flow = "name: strict\nnodes:\n  s:\n    type: shell\n    run: |\n      false | cat\n      echo unreachable\n"

    # When
    result = shell(tmp_path, flow)

    # Then
    s = result.state["nodes"]["s"]
    assert s["outcome"] == "failed"
    assert s["error"] == {"kind": "exit_code", "message": "exit code 1"}
    assert "unreachable" not in s["stdout"]


def test_given_ok_codes_and_json_output_when_run_then_output_is_parsed_and_checked(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: json-out
nodes:
  data:
    type: shell
    args: [sh, -c, 'echo "{\\"n\\": 3}"; exit 1']
    ok_codes: [0, 1]
    output: json
    output_schema:
      type: object
      required: [n]
      properties: {n: {type: integer}}
    next:
      - when: nodes.data.output.n == 3
        to: end
      - to: fail
"""

    # When
    result = shell(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    assert result.state["nodes"]["data"]["exit_code"] == 1


@pytest.mark.parametrize(
    ("script", "outcome", "kind"),
    [
        ("echo not json", "failed", "output_parse"),
        ('echo \'{"n": "three"}\'', "schema_invalid", "schema"),
    ],
)
def test_given_bad_json_output_when_run_then_the_outcome_says_why(
    tmp_path: Path, script: str, outcome: str, kind: str
) -> None:
    # Given
    flow = f"""
name: bad-json
nodes:
  data:
    type: shell
    run: {json.dumps(script)}
    output: json
    output_schema: {{type: object, properties: {{n: {{type: integer}}}}}}
"""

    # When
    result = shell(tmp_path, flow)

    # Then
    data = result.state["nodes"]["data"]
    assert (data["outcome"], data["error"]["kind"]) == (outcome, kind)


def test_given_large_output_when_run_then_state_keeps_the_tail_and_the_file_keeps_all(
    tmp_path: Path,
) -> None:
    # Given
    flow = "name: big\nnodes:\n  big:\n    type: shell\n    run: seq 1 5000\n    max_output: 100\n"

    # When
    result = shell(tmp_path, flow)

    # Then
    big = result.state["nodes"]["big"]
    assert big["stdout_truncated"] is True
    assert big["stdout"].endswith("4999\n5000\n")
    assert len(big["stdout"]) <= 100
    assert Path(big["stdout_file"]).read_text().count("\n") == 5000


def test_given_stdin_cwd_and_output_text_when_run_then_they_are_honoured(tmp_path: Path) -> None:
    # Given
    (tmp_path / "sub").mkdir()
    flow = """
name: io
nodes:
  io:
    type: shell
    run: "cat; pwd"
    stdin: "from stdin\\n"
    cwd: sub
    output: text
"""

    # When
    result = shell(tmp_path, flow)

    # Then
    output = result.state["nodes"]["io"]["output"]
    assert output.startswith("from stdin\n")
    assert output.strip().endswith("/sub")


def test_given_runner_environment_when_command_runs_then_only_allowed_variables_reach_it(
    tmp_path: Path,
) -> None:
    # Given
    environ = {
        **ENV,
        "SECRET_TOKEN": "s3cret",
        "CLAUDECODE": "1",
        "CODEX_SANDBOX_NETWORK_DISABLED": "1",
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_DIR": "/elsewhere",
        "NPM_TOKEN": "npm",
    }
    flow = "name: env\nnodes:\n  e:\n    type: shell\n    run: env\n    env: {EXTRA: yes}\n"

    # When
    result = shell(tmp_path, flow, environ=environ, config_text="env_passthrough: [NPM_TOKEN]\n")

    # Then
    names = {line.split("=", 1)[0] for line in result.state["nodes"]["e"]["stdout"].splitlines()}
    assert {"PATH", "HOME", "GIT_AUTHOR_NAME", "NPM_TOKEN", "EXTRA"} <= names
    assert {"FLOXIM_RUN_ID", "FLOXIM_NODE_ID", "FLOXIM_VISIT", "FLOXIM_ATTEMPT"} <= names
    assert not names & {"SECRET_TOKEN", "CLAUDECODE", "CODEX_SANDBOX_NETWORK_DISABLED", "GIT_DIR"}


def test_given_process_tree_ignoring_sigint_when_timed_out_then_the_whole_group_is_stopped(
    tmp_path: Path,
) -> None:
    # Given
    flow = """
name: stubborn
nodes:
  hang:
    type: shell
    run: |
      trap '' INT
      sleep 30 &
      echo started
      wait
    timeout: 1s
"""

    # When
    start = time.monotonic()
    result = shell(tmp_path, flow, grace=0.5)
    elapsed = time.monotonic() - start

    # Then
    hang = result.state["nodes"]["hang"]
    assert hang["outcome"] == "timed_out"
    assert hang["stdout"] == "started\n"
    finished = next(e for e in result.events if e["type"] == "attempt_finished")
    assert finished["data"]["stopped_by"] in ("sigterm", "sigkill")
    assert elapsed < 1 + 0.5 + 5 + 2


def test_given_flaky_command_when_retried_then_a_later_attempt_can_succeed(tmp_path: Path) -> None:
    # Given
    flow = """
name: flaky
nodes:
  flaky:
    type: shell
    run: |
      n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo $n > count
      [ "$n" -ge 3 ]
    retry: {max_attempts: 5, on: [failed]}
"""

    # When
    result = shell(tmp_path, flow)

    # Then
    assert result.status == "succeeded"
    assert result.state["nodes"]["flaky"]["attempts"] == 3


def test_given_missing_program_when_run_then_the_outcome_is_spawn_failed(tmp_path: Path) -> None:
    # Given
    flow = "name: missing\nnodes:\n  m:\n    type: shell\n    args: [no-such-program-xyz]\n"

    # When
    result = shell(tmp_path, flow)

    # Then
    assert result.state["nodes"]["m"]["error"]["kind"] == "spawn_failed"


def test_given_redact_patterns_when_command_prints_secrets_then_logs_and_events_are_masked(
    tmp_path: Path,
) -> None:
    # Given
    flow = "name: leak\nnodes:\n  leak:\n    type: shell\n    run: echo token=sk-abcdefghijklmnopqrstuvwxyz\n"

    # When
    result = shell(tmp_path, flow, config_text="redact: ['sk-[a-z]{20,}']\n")

    # Then
    leak = result.state["nodes"]["leak"]
    assert leak["stdout"] == "token=[REDACTED]\n"
    assert "sk-abc" not in Path(leak["stdout_file"]).read_text()
    assert "sk-abc" not in (result.run.path / "events.jsonl").read_text()


FIX_LOOP = """
name: fix-loop
nodes:
  implement:
    type: shell
    run: echo attempt >> attempts.txt
    next: test
  test:
    type: shell
    run: "[ $(wc -l < attempts.txt) -ge 3 ]"
    on_error: continue
    next:
      - when: nodes.test.exit_code == 0
        to: end
      - when: visits.implement < 5
        to: implement
      - to: fail
        reason: still failing
"""


def test_given_fix_loop_when_tests_fail_twice_then_the_loop_repeats_until_they_pass(
    tmp_path: Path,
) -> None:
    # When
    result = shell(tmp_path, FIX_LOOP)

    # Then
    assert result.status == "succeeded"
    assert result.visited() == ["implement", "test"] * 3
    assert [v["exit_code"] for v in result.state["nodes"]["test"]["visits"]] == [1, 1, 0]
