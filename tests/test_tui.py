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
from textual.widgets import Collapsible, DataTable, Label, ListView, Static, TabbedContent, TabPane

from whisperwind import runinfo
from whisperwind.clock import Clock, parse_iso
from whisperwind.config import load_config
from whisperwind.engine.human import Answer
from whisperwind.engine.respond import cancel, respond
from whisperwind.engine.runner import Runner
from whisperwind.store.events import read_log
from whisperwind.store.rundir import RunDir
from whisperwind.testing import VirtualClock, run_virtual
from whisperwind.tui.app import WhisperwindApp
from whisperwind.tui.common import (
    GraphView,
    NavKey,
    local_time,
    local_timestamp,
)
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
        # The waiting visit is selected, and its prompt opens.
        assert screen.query_one("#inspector", TabbedContent).active == "tab-prompt"
        prompt = text_of(screen.query_one("#inspect-prompt", Static))
        assert "Ship the demo?" in prompt and "ship / stop" in prompt
        assert text_of(screen.query_one("#limits", Static)).startswith("$0.10 / $25")

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
        assert "answer   ship" in text_of(screen.query_one("#inspect-prompt", Static))

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


def run_of(project: Path, flow: str) -> str:
    config, _ = load_config(project)
    return next(p.name for p in config.runs_dir.iterdir() if f"-{flow}-" in p.name)


def test_given_a_waiting_run_when_opened_then_its_current_step_and_visit_show_waiting(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert "  ·  at approve" in text_of(screen.query_one("#summary", Static))
        labels = [str(item.query_one(Label).render()) for item in screen.query(VisitItem)]
        assert labels[-1].startswith("… approve #1")

    drive(WhisperwindApp(config, target=run_of(project, "demo")), scenario)


STORY = """name: story
nodes:
  plan:
    type: agent
    harness: fake
    prompt: Plan.
    harness_options: {responses: [{text: planned, cost_usd: 0.1}]}
    next: flaky
  flaky:
    type: agent
    harness: fake
    prompt: Try.
    retry: {max_attempts: 3}
    harness_options:
      responses: [{outcome: failed}, {outcome: failed}, {text: done}]
    next: broken
  broken:
    type: agent
    harness: fake
    prompt: Break.
    on_error: continue
    harness_options: {responses: [{outcome: failed}]}
    next: ask
  ask:
    type: human
    message: Go on?
"""


def test_given_a_run_when_opened_then_each_visit_says_what_it_cost_and_where_it_went(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)
    starts = [
        local_time(e["ts"]) for e in read_log(run.events).events if e["type"] == "visit_started"
    ]

    async def scenario(pilot: Any) -> None:
        labels = [str(item.query_one(Label).render()) for item in pilot.app.screen.query(VisitItem)]
        # Each label split around its start time, which lines up in one column.
        split = [label.partition(start) for label, start in zip(labels, starts, strict=True)]
        assert [head for head, _, _ in split] == [
            "✓ plan #1    ",
            "✓ flaky #1   ",
            "✗ broken #1  ",
            "… ask #1     ",
        ]
        assert [tail for _, _, tail in split] == [
            "  0s  $0.10  → flaky",
            "  0s  3 attempts  → broken",
            "  0s  continued → ask",
            "",
        ]

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_finished_run_when_opened_then_no_current_step_or_failure_shows(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert " at " not in text_of(screen.query_one("#summary", Static))
        assert not screen.query_one("#failure", Static).display

    drive(WhisperwindApp(config, target=run_of(project, "quick")), scenario)


def test_given_a_run_far_from_its_limits_when_opened_then_they_show_in_one_line_without_bars(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        limits = text_of(pilot.app.screen.query_one("#limits", Static))
        assert limits == "$0.10 / $25  ·  0 / 10M tokens  ·  1 / 200 steps  ·  0s / 8h"

    drive(WhisperwindApp(config, target=run_of(project, "demo")), scenario)


def test_given_a_run_past_half_a_limit_when_opened_then_that_limit_has_a_bar(
    tmp_path: Path,
) -> None:
    tight = FLOW.replace(
        "name: demo\n", "name: demo\nlimits: {budget: {usd: 0.15, tokens: none}, max_steps: 3}\n"
    )
    run = run_flow(tmp_path, tight).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        segments = text_of(pilot.app.screen.query_one("#limits", Static)).split("  ·  ")
        assert segments == [
            "$0.10 / $0.15 ━━━━━━━━ 67%",
            "0 tokens",
            "1 / 3 steps",
            "0s / 8h",
        ]

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_failed_run_when_opened_then_a_callout_names_the_node_and_why(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, "name: doomed\nnodes:\n  a: {type: set, vars: {x: 1}, next: fail}\n")
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        failure = pilot.app.screen.query_one("#failure", Static)
        assert failure.display
        assert text_of(failure) == "✗ failed at a  route_fail: a routed to fail"

    assert run.status == "failed"
    drive(WhisperwindApp(config, target=run.run.id), scenario)


def test_given_a_run_with_inputs_when_opened_then_they_are_listed_collapsed(
    tmp_path: Path,
) -> None:
    flow = (
        "name: greet\ninputs:\n  who: {type: string, required: true}\n"
        "  times: {type: integer}\nnodes:\n  a: {type: set, vars: {x: 1}}\n"
    )
    run = run_flow(tmp_path, flow, inputs={"who": "world", "times": 2}).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        inputs = pilot.app.screen.query_one("#inputs", Collapsible)
        assert inputs.display and inputs.collapsed and inputs.title == "inputs (2)"
        await pilot.click("#inputs CollapsibleTitle")
        assert not inputs.collapsed
        assert text_of(inputs.query_one("#inputs-list", Static)) == 'who = "world"\ntimes = 2'

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_run_without_inputs_when_opened_then_no_inputs_row_shows(project: Path) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        assert not pilot.app.screen.query_one("#inputs", Collapsible).display

    drive(WhisperwindApp(config, target=run_of(project, "demo")), scenario)


def test_given_a_waiting_run_at_80x24_then_summary_prompt_and_limits_are_all_visible(
    project: Path,
) -> None:
    config, _ = load_config(project)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert "at approve" in text_of(screen.query_one("#summary", Static))
        for widget in ("#summary", "#banner", "#limits"):
            region = screen.query_one(widget).region
            assert region.height and region.bottom <= 23 and region.right <= 80, widget

    drive(WhisperwindApp(config, target=run_of(project, "demo")), scenario, size=(80, 24))


def styles_of(screen: Any, node: str) -> set[str]:
    """The styles on a node's label row in the run's graph."""
    graph = screen.query_one("#graph", GraphView)
    box = graph.picture.regions[node]
    start = sum(len(line) + 1 for line in graph.picture.lines[: box.row + 1]) + box.col
    return {str(span.style) for span in graph.render().spans if span.start <= start < span.end}


def node_in_view(screen: Any, node: str) -> bool:
    pane = screen.query_one("#graph-pane")
    box = screen.query_one("#graph", GraphView).picture.regions[node]
    return bool(
        pane.scroll_y <= box.row and box.row + box.height <= pane.scroll_y + pane.size.height
    )


def test_given_a_visit_selected_then_its_node_is_marked_and_unreached_nodes_are_dim(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        await select_visit(pilot, 1)  # flaky #1
        # The selected node's box has a background; no other box has one.
        assert any(" on " in f" {style}" for style in styles_of(screen, "flaky"))
        assert not any(" on " in f" {style}" for style in styles_of(screen, "plan"))
        assert any("dim" in style for style in styles_of(screen, "end"))  # still waiting at ask
        assert not any("dim" in style for style in styles_of(screen, "plan"))

    drive(WhisperwindApp(config, target=run.id), scenario)


CHAIN = (
    "name: chain\nnodes:\n"
    + "".join(f"  s{i}: {{type: set, vars: {{x: {i}}}, next: s{i + 1}}}\n" for i in range(8))
    + "  s8: {type: human, message: Done?, choices: [yes]}\n"
)


def test_given_a_tall_graph_then_it_follows_the_current_node_and_the_selected_visit(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, CHAIN).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        await pilot.pause()
        assert node_in_view(screen, "s8") and not node_in_view(screen, "s0")
        screen.query_one("#timeline").focus()
        await pilot.press(*["up"] * 8)  # to s0 #1
        await pilot.pause()
        assert screen.selected == ("s0", 1) and node_in_view(screen, "s0")

    drive(WhisperwindApp(config, target=run.id), scenario, size=(80, 24))


def test_given_the_graph_focused_when_a_node_is_picked_then_its_latest_visit_is_selected(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, CHAIN).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        screen.query_one("#graph", GraphView).focus()
        await pilot.press(*["up"] * 8)  # s8 → s0
        await pilot.pause()
        assert screen.selected == ("s0", 1) and not screen.follow_latest
        timeline = screen.query_one("#timeline", ListView)
        assert timeline.highlighted_child.visit == ("s0", 1)
        assert node_in_view(screen, "s0")

    drive(WhisperwindApp(config, target=run.id), scenario, size=(80, 24))


def test_given_a_node_without_visits_when_clicked_then_no_visit_is_selected(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run  # waiting at `ask`; `end` not reached
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        box = screen.query_one("#graph", GraphView).picture.regions["end"]
        await pilot.click("#graph", offset=(box.col + 2, box.row + 1))
        await pilot.pause()
        assert screen.selected is None
        assert screen.query_one("#timeline", ListView).index is None
        assert any(" on " in f" {style}" for style in styles_of(screen, "end"))
        assert "end has not run" in text_of(screen.query_one("#inspect-output", Static))
        # Picking a visited node again selects its visit.
        box = screen.query_one("#graph", GraphView).picture.regions["plan"]
        await pilot.click("#graph", offset=(box.col + 2, box.row + 1))
        await pilot.pause()
        assert screen.selected == ("plan", 1)

    drive(WhisperwindApp(config, target=run.id), scenario)


FOLLOW = FLOW.replace(
    "    choices: [ship, stop]\n",
    "    choices: [ship, stop]\n    next: done\n  done: {type: set, vars: {x: 1}}\n",
)


@pytest.mark.parametrize("picked", [False, True])
def test_given_a_run_moving_on_then_the_selection_follows_unless_a_visit_was_picked(
    tmp_path: Path, picked: bool
) -> None:
    run = run_flow(tmp_path, FOLLOW).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        if picked:
            await select_visit(pilot, 0)  # plan #1
        respond(run, "approve", Answer(choice="ship"), clock=Clock(), project_root=tmp_path,
                continue_run=False)  # fmt: skip
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: run_virtual(
                Runner(run, config, VirtualClock(), heartbeat=False, environ={}).run()
            ),
        )
        await pilot.pause(1.0)
        assert screen.selected == (("plan", 1) if picked else ("done", 1))

    drive(WhisperwindApp(config, target=run.id), scenario)


MAPPER = """name: mapper
nodes:
  each:
    type: map
    items: [1, 2]
    flow: item.yaml
    inputs: {n: "${{ item }}"}
"""
ITEM = """name: item
inputs:
  n: {type: integer, required: true}
nodes:
  note: {type: set, vars: {n: "${{ inputs.n }}"}}
"""
ASKING_ITEM = ITEM.replace(
    '  note: {type: set, vars: {n: "${{ inputs.n }}"}}\n',
    "  ask: {type: human, message: Keep it?, choices: [yes, no]}\n",
)


def children_of(run: RunDir) -> list[str]:
    return [e["data"]["run_id"] for e in read_log(run.events).events if e["type"] == "child_run"]


def visit_labels(screen: Any) -> list[str]:
    return [str(item.query_one(Label).render()) for item in screen.query(VisitItem)]


def test_given_a_map_run_when_opened_then_its_child_runs_nest_under_its_visit(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, MAPPER, files={"item.yaml": ITEM}).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        labels = visit_labels(pilot.app.screen)
        assert len(labels) == 3 and labels[0].startswith("✓ each #1")
        assert labels[1].startswith("   ↳ ✓ item [0]  0s")
        assert labels[2].startswith("   ↳ ✓ item [1]  0s")

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_subflow_run_when_opened_then_its_child_run_nests_without_an_index(
    tmp_path: Path,
) -> None:
    flow = "name: outer\nnodes:\n  sub: {type: subflow, flow: item.yaml, inputs: {n: 1}}\n"
    run = run_flow(tmp_path, flow, files={"item.yaml": ITEM}).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        assert visit_labels(pilot.app.screen)[1].startswith("   ↳ ✓ item  0s")

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_child_run_row_when_chosen_then_the_child_run_opens(tmp_path: Path) -> None:
    run = run_flow(tmp_path, MAPPER, files={"item.yaml": ITEM}).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        parent = pilot.app.screen
        await pilot.click(list(parent.query(VisitItem))[2])
        await pilot.pause()
        child = pilot.app.screen
        assert isinstance(child, RunDetailScreen) and child.run.id == children_of(run)[1]
        await pilot.press("escape")
        assert pilot.app.screen is parent

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_waiting_child_when_it_is_cancelled_elsewhere_then_its_row_follows(
    tmp_path: Path,
) -> None:
    flow = MAPPER.replace("items: [1, 2]", "items: [1]")
    run = run_flow(tmp_path, flow, files={"item.yaml": ASKING_ITEM}).run
    config, _ = load_config(tmp_path)
    child = RunDir(config.runs_dir / children_of(run)[0])

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert visit_labels(screen)[1].startswith("   ↳ … item [0]  at ask")
        cancel(child, by="ada", reason=None, clock=Clock())
        await pilot.pause(1.0)
        assert visit_labels(screen)[1].startswith("   ↳ ⊘ item [0]")

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


def shown_tabs(screen: Any) -> list[str]:
    tabs = screen.query_one("#inspector", TabbedContent)
    return [str(pane.id) for pane in tabs.query(TabPane) if tabs.get_tab(str(pane.id)).display]


async def select_visit(pilot: Any, index: int) -> None:
    await pilot.click(list(pilot.app.screen.query(VisitItem))[index])
    await pilot.pause()


def test_given_an_agent_visit_when_selected_then_only_its_tabs_show_and_output_opens(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        await select_visit(pilot, 0)  # plan #1
        screen = pilot.app.screen
        assert shown_tabs(screen) == ["tab-prompt", "tab-output", "tab-activity", "tab-usage"]
        assert screen.query_one("#inspector", TabbedContent).active == "tab-output"
        assert text_of(screen.query_one("#inspect-output", Static)) == "planned"
        usage = text_of(screen.query_one("#inspect-usage", Static))
        assert "cost" in usage and "$0.10" in usage

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_tab_picked_when_another_visit_is_selected_then_it_stays_open_where_shown(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        tabs = pilot.app.screen.query_one("#inspector", TabbedContent)
        await select_visit(pilot, 0)  # plan #1, an agent
        await pilot.click(f"#{tabs.get_tab('tab-usage').id}")
        assert tabs.active == "tab-usage"
        await select_visit(pilot, 3)  # ask #1, a human step: no usage
        assert tabs.active == "tab-prompt"
        await select_visit(pilot, 1)  # flaky #1, an agent again
        assert tabs.active == "tab-usage"

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_the_tab_the_screen_opened_when_picked_back_then_it_counts_as_a_pick(
    tmp_path: Path,
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        tabs = pilot.app.screen.query_one("#inspector", TabbedContent)
        await select_visit(pilot, 0)  # plan #1 opens on output
        await pilot.click(f"#{tabs.get_tab('tab-usage').id}")
        await pilot.click(f"#{tabs.get_tab('tab-output').id}")
        await select_visit(pilot, 1)
        assert tabs.active == "tab-output"

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_failed_shell_visit_when_opened_then_its_logs_open(tmp_path: Path) -> None:
    flow = "name: sh\nnodes:\n  check:\n    type: shell\n    run: echo out; echo oops >&2; exit 3\n"
    run = run_flow(tmp_path, flow, virtual=False).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        assert shown_tabs(screen) == ["tab-output", "tab-logs"]
        assert screen.query_one("#inspector", TabbedContent).active == "tab-logs"
        assert "oops" in text_of(screen.query_one("#inspect-logs", Static))
        assert "exit_code" in text_of(screen.query_one("#inspect-output", Static))

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_failure_routed_by_on_error_when_opened_then_its_visit_says_so(
    tmp_path: Path,
) -> None:
    flow = STORY.replace("    on_error: continue\n", "    on_error: ask\n")
    run = run_flow(tmp_path, flow).run
    config, _ = load_config(tmp_path)

    async def scenario(pilot: Any) -> None:
        labels = [str(item.query_one(Label).render()) for item in pilot.app.screen.query(VisitItem)]
        assert labels[2].startswith("✗ broken #1") and labels[2].endswith("  on_error → ask")

    drive(WhisperwindApp(config, target=run.id), scenario)


def test_given_a_run_with_retries_and_a_handled_failure_then_it_matches_the_snapshot(
    snap_compare: Any, pinned_time: None, tmp_path: Path
) -> None:
    run = run_flow(tmp_path, STORY).run
    config, _ = load_config(tmp_path)
    assert snap_compare(WhisperwindApp(config, target=run.id), terminal_size=(120, 36))


def test_given_no_runs_when_the_tui_opens_then_it_matches_the_snapshot(
    snap_compare: Any, pinned_time: None, tmp_path: Path
) -> None:
    (tmp_path / ".whisperwind").mkdir()
    config, _ = load_config(tmp_path)
    assert snap_compare(WhisperwindApp(config), terminal_size=(120, 36))
