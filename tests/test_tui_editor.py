"""Editing a flow from the TUI (spec §10.3, §10.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_human_node import cli, project
from test_tui import drive, text_of
from test_tui_control import run_dir, wait_until
from textual.widgets import ListView, Select, Static, TextArea

from arcflow import edit
from arcflow.config import load_config
from arcflow.store.events import read_log
from arcflow.tui.app import ArcflowApp
from arcflow.tui.editor import ConfirmModal
from arcflow.tui.screens import FlowGraphScreen

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
    """Open a form with `key`, fill it, and press OK."""
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
    # Given: the same flow twice, one for the TUI and one for `arcflow flow`
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
    drive(ArcflowApp(config, target=str(root / "flow.yaml")), scenario)
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
    # Given: `arcflow flow` has no operation for these, so compare with `arcflow.edit`
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
    drive(ArcflowApp(config, target=str(root / "flow.yaml")), scenario)
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

    drive(ArcflowApp(config, target=str(path)), scenario)
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

    drive(ArcflowApp(config, target=str(path)), scenario)
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

    drive(ArcflowApp(config, target=str(path)), scenario)


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

    drive(ArcflowApp(config, target=run.id), scenario)

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

    drive(ArcflowApp(config, target=run.id), scenario)
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

    drive(ArcflowApp(config, target=str(path)), scenario)
    assert path.read_text() == FLOW
