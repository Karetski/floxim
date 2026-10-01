"""`whisperwind run` on the command line: exit codes and the --json document."""

import json
from pathlib import Path

import pytest

from whisperwind import cli

OK_FLOW = """
name: ok
inputs:
  n: {type: integer, required: true}
nodes:
  a: {type: set, vars: {x: '${{ inputs.n }}'}}
outputs:
  x: ${{ vars.x }}
"""
FAIL_FLOW = """
name: nope
nodes:
  a:
    type: condition
    next:
      - when: 'false'
        to: end
      - to: fail
        reason: always
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".whisperwind").mkdir()
    (tmp_path / "ok.yaml").write_text(OK_FLOW)
    (tmp_path / "nope.yaml").write_text(FAIL_FLOW)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_given_succeeding_flow_when_run_with_json_then_exits_0_with_outputs(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # When
    code = cli.main(["run", "ok.yaml", "--input", "n=7", "--json"])

    # Then
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["data"]["status"] == "succeeded"
    assert document["data"]["outputs"] == {"x": 7}
    run_dir = project / ".whisperwind" / "runs" / document["data"]["run_id"]
    assert (run_dir / "events.jsonl").exists()


def test_given_failing_flow_when_run_then_exits_1_and_reports_why(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # When
    code = cli.main(["run", "nope.yaml"])

    # Then
    assert code == 1
    assert "run failed: always" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["ok.yaml"], ["ok.yaml", "--input", "n=seven"], ["ok.yaml", "--input", "m=1"]],
    ids=["missing-required", "not-an-integer", "undeclared"],
)
def test_given_bad_inputs_when_run_then_exits_3_before_creating_a_run(
    project: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # When
    code = cli.main(["run", *args, "--json"])

    # Then
    assert code == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "E-INVALID-INPUT"
    assert not (project / ".whisperwind" / "runs").exists()


def test_given_invalid_flow_when_run_then_exits_3_with_problems(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    (project / "bad.yaml").write_text("name: bad\nnodes:\n  a: {type: set, next: nowhere}\n")

    # When
    code = cli.main(["run", "bad.yaml"])

    # Then
    assert code == 3
    assert "E-UNKNOWN-TARGET" in capsys.readouterr().err


def test_given_events_flag_when_run_then_stdout_is_one_event_per_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # When
    code = cli.main(["run", "ok.yaml", "--input", "n=1", "--events"])

    # Then
    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_succeeded"


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, check=True, capture_output=True,
    )  # fmt: skip


def test_given_flow_not_tracked_by_git_when_run_then_a_notice_says_to_review_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: ok.yaml is committed, nope.yaml is not
    _git(project, "init", "-q")
    _git(project, "add", "ok.yaml")
    _git(project, "commit", "-qm", "ok")

    # When
    cli.main(["run", "nope.yaml"])
    untracked = capsys.readouterr().err
    cli.main(["run", "ok.yaml", "--input", "n=1"])
    tracked = capsys.readouterr().err

    # Then
    assert "nope.yaml is not tracked by git" in untracked
    assert "not tracked" not in tracked


def test_given_project_outside_git_when_run_then_no_notice(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["run", "ok.yaml", "--input", "n=1"])
    assert "not tracked" not in capsys.readouterr().err
