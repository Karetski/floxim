"""The terminal UI, driven with Textual's pilot."""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from engine_support import run_flow
from textual.widgets import DataTable, Label, ListView, Static

from whisperwind import runinfo
from whisperwind.clock import Clock, parse_iso
from whisperwind.config import load_config
from whisperwind.engine.human import Answer
from whisperwind.engine.respond import respond
from whisperwind.engine.runner import Runner
from whisperwind.store.events import read_log
from whisperwind.store.rundir import RunDir
from whisperwind.testing import VirtualClock, run_virtual
from whisperwind.tui.app import WhisperwindApp
from whisperwind.tui.common import NavKey, local_time, local_timestamp
from whisperwind.tui.screens import (
    FlowGraphScreen,
    FlowsScreen,
    RunDetailScreen,
    RunsScreen,
    VisitItem,
)

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


def drive(
    app: WhisperwindApp,
    scenario: Callable[[Any], Awaitable[None]],
    size: tuple[int, int] = (140, 45),
) -> None:
    async def main() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(main())


def text_of(widget: Static) -> str:
    return str(widget.render())


@pytest.fixture
def local_zone() -> Iterator[None]:
    """Pins the local timezone to UTC+3, named FLX, whatever the machine's zone is."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "FLX-3"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def in_zone(stamp: str, form: str) -> str:
    """A stored UTC timestamp as the pinned zone's clock shows it."""
    return (parse_iso(stamp) + datetime.timedelta(hours=3)).strftime(form)


def compact_in_zone(stamp: str) -> set[str]:
    """Both compact forms of a timestamp in the pinned zone: which one shows depends on
    whether it falls on the real today."""
    return {in_zone(stamp, "%H:%M:%S"), in_zone(stamp, "%b %d %H:%M")}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    run_flow(tmp_path, QUICK)
    run_flow(tmp_path, FLOW)  # stops waiting at `approve`
    return tmp_path


def run_like_cli(app: WhisperwindApp, scenario: Callable[[Any], Awaitable[None]]) -> int | None:
    """Run the app with `App.run`, as `whisperwind tui` does, and return its exit code.

    Unlike the pilot's `run_test`, `App.run` starts tasks eagerly, so a new screen composes
    before the switch that created it has finished."""

    async def auto_pilot(pilot: Any) -> None:
        await pilot.pause()
        await scenario(pilot)
        if pilot.app.return_code is None:  # set when the app crashed
            pilot.app.exit()

    app.run(headless=True, auto_pilot=auto_pilot, size=(80, 24))
    return app.return_code


def test_given_a_project_when_the_tui_starts_as_whisperwind_tui_does_then_it_runs(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def nothing(pilot: Any) -> None:
        pass

    assert run_like_cli(WhisperwindApp(config), nothing) == 0


def test_given_views_when_switching_between_them_then_the_current_ones_key_is_highlighted(
    project: Path,
) -> None:
    config, _ = load_config(project)
    highlighted: list[list[str]] = []

    async def switch(pilot: Any) -> None:
        for key in ("f", "r", "f"):
            await pilot.press(key)
            await pilot.pause()
            keys = pilot.app.screen.query(NavKey)
            highlighted.append([str(k.render()).strip() for k in keys if k.has_class("-active")])

    assert run_like_cli(WhisperwindApp(config), switch) == 0
    assert highlighted == [["f Flows"], ["r Runs"], ["f Flows"]]


def test_given_a_utc_timestamp_when_shown_then_it_is_in_the_local_timezone(
    local_zone: None,
) -> None:
    stamp = "2026-09-30T22:30:00.000Z"  # 01:30 on Oct 1 in UTC+3
    assert local_timestamp(stamp) == "2026-10-01 01:30:00 FLX"
    # Today is the local day, not the UTC one.
    assert local_time(stamp, now=parse_iso("2026-10-01T05:00:00Z")) == "01:30:00"
    assert local_time(stamp, now=parse_iso("2026-10-02T05:00:00Z")) == "Oct 01 01:30"


def test_given_runs_when_listed_then_start_and_finish_show_in_local_time(
    project: Path, local_zone: None
) -> None:
    config, _ = load_config(project)
    runs = {run.id: runinfo.summary(run, Clock()) for run in map(RunDir, config.runs_dir.iterdir())}

    async def scenario(pilot: Any) -> None:
        table = pilot.app.screen.query_one("#runs", DataTable)
        labels = [str(column.label) for column in table.columns.values()]
        assert labels[4:7] == ["started", "finished", "duration"]
        for run_id, info in runs.items():
            started, finished = table.get_row(run_id)[4:6]
            assert started in compact_in_zone(info["started_at"])
            if info["status"] == "waiting":
                assert finished == ""
            else:
                assert finished in compact_in_zone(info["finished_at"])

    drive(WhisperwindApp(config), scenario)


def test_given_runs_at_80_columns_when_scrolled_right_then_status_and_run_stay(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        table = pilot.app.screen.query_one("#runs", DataTable)
        assert table.virtual_size.width > 80
        table.scroll_to(x=table.max_scroll_x, animate=False)
        await pilot.pause()
        row = table.render_line(1).text
        assert table.get_row_at(0)[1] in row and "waiting" in row

    drive(WhisperwindApp(config), scenario, size=(80, 24))


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

    drive(WhisperwindApp(config), scenario)


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

    drive(WhisperwindApp(config), scenario)


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

    drive(WhisperwindApp(config, target=waiting.name), scenario)


@pytest.mark.parametrize("flow", ["demo", "quick"])
def test_given_a_run_when_opened_then_its_summary_and_timeline_show_local_times(
    project: Path, local_zone: None, flow: str
) -> None:
    config, _ = load_config(project)
    run = next(r for r in map(RunDir, config.runs_dir.iterdir()) if f"-{flow}-" in r.id)
    info = runinfo.summary(run, Clock())
    full = "%Y-%m-%d %H:%M:%S FLX"
    starts = [e["ts"] for e in read_log(run.events).events if e["type"] == "visit_started"]

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        summary = text_of(screen.query_one("#summary", Static))
        assert f"started {in_zone(info['started_at'], full)}" in summary
        if flow == "demo":  # still waiting
            assert "finished" not in summary and "took" not in summary
        else:
            assert f"finished {in_zone(info['finished_at'], full)}  ·  took " in summary
        labels = [str(item.query_one(Label).render()) for item in screen.query(VisitItem)]
        for label, start in zip(labels, starts, strict=True):
            assert any(shown in label for shown in compact_in_zone(start))

    drive(WhisperwindApp(config, target=run.id), scenario)


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

    drive(WhisperwindApp(config), scenario)


def test_given_lists_when_switching_runs_flows_runs_then_each_keeps_its_cursor_and_filter(
    project: Path,
) -> None:
    (project / "flows").mkdir()
    (project / "flows" / "demo.yaml").write_text(FLOW)
    (project / "flows" / "quick.yaml").write_text(QUICK)
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        runs = pilot.app.screen
        await pilot.press("down", "f", "down", "r")
        assert pilot.app.screen is runs
        assert runs.query_one("#runs", DataTable).cursor_row == 1
        await pilot.press("s", "f")
        flows = pilot.app.screen
        assert isinstance(flows, FlowsScreen)
        assert flows.query_one("#flows", DataTable).cursor_row == 1
        await pilot.press("r")
        assert runs.status_filter == "waiting"
        assert runs.query_one("#runs", DataTable).row_count == 1

    drive(WhisperwindApp(config), scenario)


def test_given_a_flow_open_when_switching_away_and_back_then_it_is_still_open(
    project: Path,
) -> None:
    (project / "flows").mkdir()
    (project / "flows" / "demo.yaml").write_text(FLOW)
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        await pilot.press("f", "enter", "r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RunsScreen)
        await pilot.press("f")
        assert isinstance(pilot.app.screen, FlowGraphScreen)
        await pilot.press("f")  # again, from inside Flows: back to its list
        assert isinstance(pilot.app.screen, FlowsScreen)

    drive(WhisperwindApp(config), scenario)


def test_given_runs_when_the_pinned_flows_key_is_clicked_then_flows_show(project: Path) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        flows_key = next(k for k in pilot.app.screen.query(NavKey) if k.action == "show_flows")
        await pilot.click(flows_key)
        await pilot.pause()
        assert isinstance(pilot.app.screen, FlowsScreen)

    drive(WhisperwindApp(config), scenario)


@pytest.mark.parametrize("view", ["runs", "flows"])
def test_given_a_drilled_in_screen_at_80x24_then_the_root_views_stay_pinned_in_the_footer(
    project: Path, view: str
) -> None:
    flow = project / "flow.yaml"
    flow.write_text(FLOW)
    config, _ = load_config(project)
    run_id = next(p.name for p in config.runs_dir.iterdir() if "demo" in p.name)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen if view == "runs" else FlowGraphScreen)
        pinned = {str(key.render()).strip(): key for key in screen.query(NavKey)}
        assert list(pinned) == ["r Runs", "f Flows"]
        for key in pinned.values():
            assert key.region.width and key.region.right <= 80 and key.region.y == 23
        assert [d for d, key in pinned.items() if key.has_class("-active")] == [
            {"runs": "r Runs", "flows": "f Flows"}[view]
        ]

    target = run_id if view == "runs" else str(flow)
    drive(WhisperwindApp(config, target=target), scenario, size=(80, 24))


def test_given_flow_file_edited_while_shown_then_the_graph_follows_and_survives_bad_saves(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "flow.yaml"
    path.write_text(QUICK)
    (tmp_path / ".whisperwind").mkdir()
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

    drive(WhisperwindApp(config, target=str(path)), scenario)


def test_given_flow_graph_screen_when_rendered_then_it_matches_the_snapshot(
    snap_compare: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = Path(__file__).parent / "flows" / "valid"
    monkeypatch.chdir(corpus)
    (tmp_path / ".whisperwind").mkdir()
    config, _ = load_config(tmp_path)
    assert snap_compare(
        WhisperwindApp(config, target="implement-feature.yaml"), terminal_size=(120, 50)
    )


SCREENS: dict[str, tuple[str, ...]] = {
    "runs": (),
    "run-waiting": ("enter",),
    "run-succeeded": ("down", "enter"),
    "answer": ("a",),
    "flows": ("f",),
    "flow-with-problems": ("f", "enter"),
    "add-node": ("f", "down", "enter", "n"),
}


@pytest.fixture
def pinned_time(monkeypatch: pytest.MonkeyPatch, local_zone: None) -> None:
    """Run IDs and the TUI's clock fixed, so screens render the same on every machine."""
    suffixes = iter("abcdefghijklmnop")
    monkeypatch.setattr("whisperwind.store.ids.secrets.choice", lambda _: next(suffixes))
    now = datetime.datetime(2026, 9, 30, 12, 5, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(Clock, "now", lambda self: now)


@pytest.mark.parametrize("screen", list(SCREENS))
def test_given_a_project_when_a_screen_is_shown_then_it_matches_the_snapshot(
    snap_compare: Any,
    pinned_time: None,
    project: Path,
    screen: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / "flows").mkdir()
    (project / "flows" / "demo.yaml").write_text(FLOW)
    (project / "flows" / "broken.yaml").write_text(
        "name: broken\nnodes:\n  a: {type: set, next: nowhere}\n"
    )
    monkeypatch.chdir(project)  # flow paths show relative to the project, not tmp_path
    config, _ = load_config(Path("."))
    assert snap_compare(WhisperwindApp(config), press=SCREENS[screen], terminal_size=(120, 36))


def test_given_no_runs_when_the_tui_opens_then_it_matches_the_snapshot(
    snap_compare: Any, pinned_time: None, tmp_path: Path
) -> None:
    (tmp_path / ".whisperwind").mkdir()
    config, _ = load_config(tmp_path)
    assert snap_compare(WhisperwindApp(config), terminal_size=(120, 36))
