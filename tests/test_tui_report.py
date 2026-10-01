"""The CI report of TUI snapshots a change adds, changes or removes."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from tui_report import MARKER, report, svg_to_text


def screen_svg(*lines: str) -> str:
    console = Console(width=30, file=io.StringIO(), record=True, force_terminal=True)
    for line in lines:
        console.print(Text.from_markup(line))
    return console.export_svg()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit(cwd: Path, *args: str) -> None:
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", *args)


def test_given_a_styled_screen_svg_when_converted_then_text_keeps_its_columns() -> None:
    svg = screen_svg("[bold]run[/]  [red]✗ failed[/]", "  └─ 日本 ok")

    assert svg_to_text(svg) == "run  ✗ failed\n  └─ 日本 ok"


def test_given_snapshots_added_changed_and_removed_when_reported_then_each_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a base commit, then a branch that changes, adds and removes snapshots
    snapshots = tmp_path / "tests" / "__snapshots__" / "test_tui"
    snapshots.mkdir(parents=True)
    (snapshots / "runs.raw").write_text(screen_svg("status", "… waiting"))
    (snapshots / "gone.raw").write_text(screen_svg("old screen"))
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    commit(tmp_path, "-m", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    (snapshots / "runs.raw").write_text(screen_svg("status", "✓ succeeded"))
    (snapshots / "flows.raw").write_text(screen_svg("flow  demo"))
    (snapshots / "gone.raw").unlink()
    git(tmp_path, "add", "-A")
    commit(tmp_path, "-m", "feature")
    monkeypatch.chdir(tmp_path)

    # When
    markdown = report("main", "passed", "https://ci.example/1")

    # Then
    assert markdown.startswith(MARKER)
    assert "✅ Tests passed · [run](https://ci.example/1)" in markdown
    assert "<code>test_tui/flows</code> — added" in markdown and "flow  demo" in markdown
    assert "<code>test_tui/runs</code> — changed" in markdown
    assert "-… waiting\n+✓ succeeded" in markdown
    assert "- <code>test_tui/gone</code> — removed" in markdown


def test_given_no_snapshot_changes_when_tests_failed_then_the_report_says_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "--allow-empty", "-m", "base")
    monkeypatch.chdir(tmp_path)

    markdown = report("main", "failed", None)

    assert "❌ Tests failed" in markdown and "snapshot-report" in markdown
    assert "No screen changed." in markdown
