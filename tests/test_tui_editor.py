"""Editing a flow from the TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_human_node import cli, project
from test_tui import drive, styles_of, text_of
from test_tui_control import run_dir, wait_until
from textual.widgets import ListView, Select, Static, TextArea

from whisperwind import edit
from whisperwind.config import load_config
from whisperwind.store.events import read_log
from whisperwind.tui.app import WhisperwindApp
from whisperwind.tui.common import GraphView
from whisperwind.tui.editor import ConfirmModal, FieldModal
from whisperwind.tui.screens import FlowGraphScreen

FLOW = """name: pair
# Build, then record the result.
nodes:
  build:
    type: shell
    run: make
    next: done

  done:
    type: set
    vars: {ok: true}
"""
WAIT = """name: wait
nodes:
  first:
    type: set
    vars: {x: 1}
    next: approve
  approve:
    type: human
    message: Go?
    choices: [go]
"""


async def submit(pilot: Any, fields: dict[str, Any], key: str) -> None:
    """Open a form with `key` (or use the open one), fill it, and press OK."""
    if key:
        await pilot.press(key)
        await pilot.pause()
    for selector, value in fields.items():
        widget = pilot.app.screen.query_one(selector)
        if isinstance(widget, TextArea):
            widget.text = value
        else:
            widget.value = value
    await pilot.pause()
    await pilot.click("#ok")
    await pilot.pause(0.5)


def select(pilot: Any, node: str) -> None:
    screen = pilot.app.screen
    listing = screen.query_one("#node-list", ListView)
    listing.index = [item.node for item in listing.children].index(node)


def test_given_edits_made_in_the_tui_then_the_file_matches_the_same_cli_edits(
    tmp_path: Path,
) -> None:
    # Given: the same flow twice, one for the TUI and one for `whisperwind flow`
    root = project(tmp_path, FLOW)
    (tmp_path / "twin").mkdir()
    twin = project(tmp_path / "twin", FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        assert isinstance(pilot.app.screen, FlowGraphScreen)
        await submit(pilot, {"#node-id": "check", "#node-type": "shell"}, "n")
        await submit(pilot, {"#new-id": "compile"}, "R")
        await submit(pilot, {"#field": "run", "#value": "|\n  make all\n  make test\n"}, "e")
        await submit(pilot, {"#target": "check", "#when": "nodes.compile.exit_code != 0"}, "k")
        select(pilot, "check")
        await pilot.pause()
        await submit(pilot, {"#field": "run", "#value": "make lint"}, "e")
        select(pilot, "done")
        await pilot.pause()
        await submit(pilot, {"#field": "vars", "#value": ""}, "e")
        select(pilot, "check")
        await pilot.pause()
        await submit(pilot, {"#remove-edges": True}, "d")
        select(pilot, "compile")
        await pilot.pause()
        await submit(pilot, {"#target": "done"}, "x")

    # When
    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)
    for args in (
        ["add-node", "flow.yaml", "check", "--type", "shell", "--after", "build"],
        ["rename-node", "flow.yaml", "build", "compile"],
        ["set", "flow.yaml", "compile", "run", "|\n  make all\n  make test\n"],
        ["connect", "flow.yaml", "compile", "check", "--when", "nodes.compile.exit_code != 0"],
        ["set", "flow.yaml", "check", "run", "make lint"],
        ["unset", "flow.yaml", "done", "vars"],
        ["rm-node", "flow.yaml", "check", "--remove-edges"],
        ["disconnect", "flow.yaml", "compile", "done"],
    ):
        result = cli(twin, "flow", *args)
        assert result.returncode == 0, result.stderr

    # Then
    edited = (root / "flow.yaml").read_bytes()
    assert b"# Build, then record the result." in edited
    assert edited == (twin / "flow.yaml").read_bytes()


def test_given_reorder_and_change_type_in_the_tui_then_the_file_matches_the_edit_library(
    tmp_path: Path,
) -> None:
    # Given: `whisperwind flow` has no operation for these, so compare with `whisperwind.edit`
    flow = FLOW.replace(
        "    next: done\n",
        "    next:\n      - to: done\n        when: vars.fast\n"
        "      - to: build\n        when: vars.slow\n      - to: done\n",
    )
    root = project(tmp_path, flow)
    twin = tmp_path / "twin.yaml"
    twin.write_text(flow)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        await submit(pilot, {"#order": "1, 0, 2"}, "o")
        await submit(pilot, {"#node-type": "set", "#drop": True}, "t")
        await submit(pilot, {"#field": "vars", "#value": "{built: true}"}, "e")

    # When
    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)
    for operation in (
        lambda ed: edit.reorder_cases(ed, "build", [1, 0, 2]),
        lambda ed: edit.change_type(ed, "build", "set", drop=True),
        lambda ed: edit.set_field(ed, "build", "vars", {"built": True}),
    ):
        editable = edit.open_flow(twin)
        operation(editable)
        edit.save(editable)

    # Then
    assert (root / "flow.yaml").read_bytes() == twin.read_bytes()


def test_given_file_changed_while_a_form_is_open_then_the_tui_reloads_and_asks(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, FLOW)
    path = root / "flow.yaml"
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        await pilot.press("e")
        await pilot.pause()
        pilot.app.screen.query_one("#field", Select).value = "run"
        pilot.app.screen.query_one("#value", TextArea).text = "make all"
        # When: someone else edits the file before the form is saved
        path.write_text(FLOW.replace("vars: {ok: true}", "vars: {ok: false}"))
        await pilot.click("#ok")
        await pilot.pause(0.5)

        # Then: nothing is overwritten until the person agrees
        assert isinstance(pilot.app.screen, ConfirmModal)
        assert "run: make\n" in path.read_text()
        await pilot.click("#confirm-yes")
        await pilot.pause(0.5)

    drive(WhisperwindApp(config, target=str(path)), scenario)
    text = path.read_text()
    assert "run: make all" in text and "vars: {ok: false}" in text


def test_given_field_edit_that_breaks_the_flow_then_it_is_kept_only_as_a_confirmed_draft(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FLOW)
    path = root / "flow.yaml"
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        await submit(pilot, {"#field": "run", "#value": "[make, all]"}, "e")
        assert isinstance(pilot.app.screen, ConfirmModal)
        assert path.read_text() == FLOW
        await pilot.click("#confirm-yes")
        await pilot.pause(1.0)
        problems = text_of(pilot.app.screen.query_one("#problems", Static))
        assert "run" in problems

    drive(WhisperwindApp(config, target=str(path)), scenario)
    assert "run:\n    - make\n    - all\n" in path.read_text()


def test_given_node_added_by_hand_then_the_node_list_shows_it_within_a_poll(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FLOW)
    path = root / "flow.yaml"
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        listing = pilot.app.screen.query_one("#node-list", ListView)
        assert [item.node for item in listing.children] == ["build", "done"]
        path.write_text(FLOW + "  extra:\n    type: sleep\n    duration: 1s\n")
        await pilot.pause(0.6)  # two polling intervals
        assert [item.node for item in listing.children] == ["build", "done", "extra"]

    drive(WhisperwindApp(config, target=str(path)), scenario)


FORK = """name: fork
nodes:
  a:
    type: set
    vars: {x: 1}
    next:
      - when: vars.x > 0
        to: b
      - to: c
  b: {type: set, vars: {y: 1}}
  c: {type: set, vars: {z: 1}}
"""


def listed(screen: Any) -> str | None:
    """The node highlighted in the node list."""
    item = screen.query_one("#node-list", ListView).highlighted_child
    return getattr(item, "node", None)


def test_given_the_graph_focused_when_arrows_are_pressed_then_the_selection_moves_spatially(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FORK)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        graph = screen.query_one("#graph", GraphView)
        graph.focus()
        await pilot.pause()
        regions = graph.picture.regions
        left, right = sorted(("b", "c"), key=lambda node: regions[node].col)
        await pilot.press("down", "left")
        await pilot.pause()
        assert screen.node == left and graph.selected == left
        assert listed(screen) == left
        var = {"b": "y", "c": "z"}[left]
        assert f"{var}: 1" in text_of(screen.query_one("#node-config", Static))
        await pilot.press("right")
        await pilot.pause()
        assert screen.node == right and listed(screen) == right
        await pilot.press("up")
        await pilot.pause()
        assert screen.node == "a" and listed(screen) == "a"
        assert "x: 1" in text_of(screen.query_one("#node-config", Static))

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


def test_given_a_node_box_when_clicked_then_it_is_selected(tmp_path: Path) -> None:
    root = project(tmp_path, FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        box = screen.query_one("#graph", GraphView).picture.regions["done"]
        await pilot.click("#graph", offset=(box.col + 2, box.row + 1))
        await pilot.pause()
        assert screen.node == "done" and listed(screen) == "done"
        assert "ok: true" in text_of(screen.query_one("#node-config", Static))

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


def test_given_a_node_box_when_the_mouse_is_pressed_on_it_then_no_text_selection_starts(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        box = screen.query_one("#graph", GraphView).picture.regions["done"]
        await pilot.mouse_down("#graph", offset=(box.col + 2, box.row + 1))
        await pilot.hover("#graph", offset=(box.col + 4, box.row + 1))
        await pilot.pause()
        assert not screen.selections

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


def test_given_a_node_selected_in_the_list_then_the_graph_marks_it(tmp_path: Path) -> None:
    root = project(tmp_path, FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        select(pilot, "done")
        await pilot.pause()
        assert screen.query_one("#graph", GraphView).selected == "done"
        assert any(" on " in f" {style}" for style in styles_of(screen, "done"))
        assert not any(" on " in f" {style}" for style in styles_of(screen, "build"))

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


def test_given_a_node_in_the_graph_when_enter_is_pressed_then_its_fields_open_unless_built_in(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        screen.query_one("#graph", GraphView).focus()
        await pilot.press("down", "down")  # build → done → end
        await pilot.pause()
        assert screen.node == "end" and listed(screen) is None
        assert "built in" in text_of(screen.query_one("#node-config", Static))
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.screen is screen
        await pilot.press("up", "enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FieldModal)

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


def test_given_a_flow_too_big_to_lay_out_then_the_listed_graph_selects_line_by_line(
    tmp_path: Path,
) -> None:
    nodes = "".join(f"  n{i}: {{type: set, vars: {{x: {i}}}, next: n{i + 1}}}\n" for i in range(60))
    root = project(tmp_path, f"name: long\nnodes:\n{nodes}  n60: {{type: set, vars: {{x: 0}}}}\n")
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        screen = pilot.app.screen
        graph = screen.query_one("#graph", GraphView)
        assert graph.picture.fallback
        graph.focus()
        await pilot.press("down", "down")
        await pilot.pause()
        assert screen.node == "n2" and listed(screen) == "n2"

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)


@pytest.fixture
def waiting(tmp_path: Path) -> Path:
    root = project(tmp_path, WAIT)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    return root


def test_given_flow_edited_since_the_run_started_then_detail_shows_the_diff_and_reloads(
    waiting: Path,
) -> None:
    # Given: a compatible edit
    (waiting / "flow.yaml").write_text(WAIT.replace("Go?", "Go now?"))
    config, _ = load_config(waiting)
    run = run_dir(waiting)

    async def scenario(pilot: Any) -> None:
        banner = text_of(pilot.app.screen.query_one("#banner", Static))
        assert "flow changed since this run started; L resumes with it" in banner
        diff = text_of(pilot.app.screen.query_one("#inspect-flow", Static))
        assert "-    message: Go?" in diff and "+    message: Go now?" in diff
        # When
        await pilot.press("L")
        await pilot.pause()

    drive(WhisperwindApp(config, target=run.id), scenario)

    # Then
    def reloaded() -> bool:
        types = [e["type"] for e in read_log(run.events).events]
        return "flow_reloaded" in types and not run.lock.exists()

    wait_until(reloaded)
    assert "Go now?" in (run.path / run.read_state()["run"]["snapshot"]).read_text()


def test_given_edit_that_retypes_a_visited_node_then_reload_is_not_offered(
    waiting: Path,
) -> None:
    (waiting / "flow.yaml").write_text(
        WAIT.replace("type: set\n    vars: {x: 1}", "type: sleep\n    duration: 1s")
    )
    config, _ = load_config(waiting)
    run = run_dir(waiting)

    async def scenario(pilot: Any) -> None:
        banner = text_of(pilot.app.screen.query_one("#banner", Static))
        assert "it cannot replace this run's flow" in banner
        await pilot.press("L")
        await pilot.pause()

    drive(WhisperwindApp(config, target=run.id), scenario)
    assert "flow_reloaded" not in [e["type"] for e in read_log(run.events).events]
    assert not list(run.path.glob("snapshot-*"))


def test_given_add_node_form_with_a_taken_id_then_the_error_is_shown_and_the_file_kept(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, FLOW)
    path = root / "flow.yaml"
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        await submit(pilot, {"#node-id": "done"}, "n")
        assert isinstance(pilot.app.screen, FlowGraphScreen)

    drive(WhisperwindApp(config, target=str(path)), scenario)
    assert path.read_text() == FLOW


def test_given_type_that_needs_a_value_when_chosen_in_the_tui_then_the_form_asks_for_it(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, FLOW)
    twin = tmp_path / "twin.yaml"
    twin.write_text(FLOW)
    config, _ = load_config(root)

    async def scenario(pilot: Any) -> None:
        select(pilot, "done")
        await pilot.pause()
        # When: `done` becomes an agent, which needs a harness
        await pilot.press("t")
        await pilot.pause()
        pilot.app.screen.query_one("#node-type", Select).value = "agent"
        await pilot.pause()
        await submit(pilot, {"#field-harness": "fake", "#drop": True}, "")
        # and a new agent node gets one too
        await pilot.press("n")
        await pilot.pause()
        pilot.app.screen.query_one("#node-type", Select).value = "agent"
        await pilot.pause()
        await submit(pilot, {"#node-id": "review", "#field-harness": "fake"}, "")

    drive(WhisperwindApp(config, target=str(root / "flow.yaml")), scenario)
    editable = edit.open_flow(twin)
    edit.change_type(editable, "done", "agent", drop=True, fields={"harness": "fake"})
    edit.add_node(editable, "review", "agent", after="done", fields={"harness": "fake"})
    edit.save(editable)

    # Then
    assert (root / "flow.yaml").read_bytes() == twin.read_bytes()
