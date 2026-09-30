"""The terminal UI, driven with Textual's pilot (spec §10, §13)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow
from textual.widgets import DataTable, ListView, Static

from arcflow.clock import Clock
from arcflow.config import load_config
from arcflow.engine.human import Answer
from arcflow.engine.respond import respond
from arcflow.engine.runner import Runner
from arcflow.testing import VirtualClock, run_virtual
from arcflow.tui.app import ArcflowApp
from arcflow.tui.screens import FlowGraphScreen, FlowsScreen, RunDetailScreen, RunsScreen

FLOW = """name: demo
nodes:
  plan:
    type: agent
    harness: fake
    prompt: Plan the demo.
    harness_options: {responses: [{text: planned, cost_usd: 0.1}]}
    next: approve
  approve:
    type: human
    message: Ship the demo?
    choices: [ship, stop]
"""
QUICK = "name: quick\nnodes:\n  a: {type: set, vars: {x: 1}}\n"


def drive(app: ArcflowApp, scenario: Callable[[Any], Awaitable[None]]) -> None:
    async def main() -> None:
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(main())


def text_of(widget: Static) -> str:
    return str(widget.render())


@pytest.fixture
def project(tmp_path: Path) -> Path:
    run_flow(tmp_path, QUICK)
    run_flow(tmp_path, FLOW)  # stops waiting at `approve`
    return tmp_path


def test_given_runs_when_the_tui_opens_then_waiting_runs_come_first(project: Path) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert isinstance(screen, RunsScreen)
        table = screen.query_one("#runs", DataTable)
        assert table.row_count == 2
        assert "waiting" in str(table.get_row_at(0)[0])
        await pilot.press("s")  # filter: waiting
        assert table.row_count == 1
        await pilot.press("s", "s")  # running, then failed: nothing
        assert table.row_count == 0

    drive(ArcflowApp(config), scenario)


def test_given_waiting_run_when_opened_then_graph_banner_and_inspector_show_it(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        await pilot.press("s")  # only the waiting run
        await pilot.press("enter")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen)
        assert "Ship the demo?" in text_of(screen.query_one("#banner", Static))
        graph = text_of(screen.query_one("#graph", Static))
        assert "✓ ◆ plan" in graph and "… ☺ approve" in graph
        assert len(screen.query_one("#timeline", ListView)) == 2
        assert "Plan the demo." in text_of(screen.query_one("#inspect-prompt", Static))
        assert "~$0.10" in text_of(screen.query_one("#summary", Static))

    drive(ArcflowApp(config), scenario)


def test_given_run_that_finishes_while_open_when_refreshed_then_the_screen_follows(
    project: Path,
) -> None:
    config, _ = load_config(project)
    waiting = next(p for p in config.runs_dir.iterdir() if "demo" in p.name)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen)
        assert "waiting" in text_of(screen.query_one("#summary", Static))
        # Another process answers and finishes the run.
        from arcflow.store.rundir import RunDir

        run = RunDir(waiting)
        respond(
            run,
            "approve",
            Answer(choice="ship"),
            clock=Clock(),
            project_root=project,
            continue_run=False,
        )
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: run_virtual(
                Runner(run, config, VirtualClock(), heartbeat=False, environ={}).run()
            ),
        )
        await pilot.pause(1.0)
        assert "succeeded" in text_of(screen.query_one("#summary", Static))
        assert "✓ ☺ approve" in text_of(screen.query_one("#graph", Static))

    drive(ArcflowApp(config, target=waiting.name), scenario)


def test_given_flow_files_when_listed_then_validity_and_last_run_show(project: Path) -> None:
    (project / "flows").mkdir()
    (project / "flows" / "demo.yaml").write_text(FLOW)
    (project / "flows" / "broken.yaml").write_text(
        "name: broken\nnodes:\n  a: {type: set, next: nowhere}\n"
    )
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        await pilot.press("f")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, FlowsScreen)
        rows = [screen.query_one("#flows", DataTable).get_row_at(i) for i in range(2)]
        assert [(str(r[0]), r[1]) for r in rows] == [("✗", "broken"), ("✓", "demo")]
        assert "waiting" in str(rows[1][3])

    drive(ArcflowApp(config), scenario)


def test_given_flow_file_edited_while_shown_then_the_graph_follows_and_survives_bad_saves(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "flow.yaml"
    path.write_text(QUICK)
    (tmp_path / ".arcflow").mkdir()
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert isinstance(screen, FlowGraphScreen)
        graph = screen.query_one("#graph", Static)
        assert "= a" in text_of(graph)

        # When: a valid edit, then an invalid one
        path.write_text(
            QUICK.replace("{x: 1}}", "{x: 1}, next: b}\n  b: {type: sleep, duration: 1s}")
        )
        await pilot.pause(1.5)
        assert "◷ b" in text_of(graph)
        path.write_text("name: quick\nnodes:\n  a: {type: set, next: nowhere}\n")
        await pilot.pause(1.5)

        # Then: the last good graph stays, with the problem listed
        assert "◷ b" in text_of(graph)
        assert "E-UNKNOWN-TARGET" in text_of(screen.query_one("#problems", Static))

    drive(ArcflowApp(config, target=str(path)), scenario)


def test_given_flow_graph_screen_when_rendered_then_it_matches_the_snapshot(
    snap_compare: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = Path(__file__).parent / "flows" / "valid"
    monkeypatch.chdir(corpus)
    (tmp_path / ".arcflow").mkdir()
    config, _ = load_config(tmp_path)
    assert snap_compare(
        ArcflowApp(config, target="implement-feature.yaml"), terminal_size=(120, 50)
    )
