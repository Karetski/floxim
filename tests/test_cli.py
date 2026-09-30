"""Behaviour of the `arcflow` entry point that every command relies on."""

import subprocess
import sys
from pathlib import Path

import pytest

import arcflow
from arcflow import cli


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_when_version_requested_then_prints_name_and_version() -> None:
    # When
    result = _run(sys.executable, "-m", "arcflow", "--version")

    # Then
    assert result.returncode == 0
    assert result.stdout.strip() == f"arcflow {arcflow.__version__}"


@pytest.mark.parametrize("script", ["arcflow", "arcf"])
def test_given_installed_scripts_when_version_requested_then_both_names_work(
    script: str,
) -> None:
    # Given
    executable = Path(sys.executable).parent / script

    # When
    result = _run(str(executable), "--version")

    # Then
    assert result.returncode == 0
    assert result.stdout.strip() == f"arcflow {arcflow.__version__}"


def test_when_arguments_are_invalid_then_exits_with_usage_error() -> None:
    # When
    result = _run(sys.executable, "-m", "arcflow", "--no-such-flag")

    # Then
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_given_a_bug_when_a_command_raises_then_exits_70_and_asks_for_a_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    def broken_parser() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "build_parser", broken_parser)

    # When
    code = cli.main([])

    # Then
    assert code == 70
    err = capsys.readouterr().err
    assert "internal error" in err
    assert "boom" in err
    assert "github.com/Karetski/arcflow/issues" in err
