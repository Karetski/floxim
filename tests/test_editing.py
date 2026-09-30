"""Structured edits of flow files (spec §10.4)."""

from __future__ import annotations

import difflib
import json
import os
import stat
from pathlib import Path

import pytest

from floxim import cli, edit

FLOWS = Path(__file__).parent / "flows"
HAND = (FLOWS / "edit" / "hand.yaml").read_text()
CORPUS = sorted(
    [*FLOWS.glob("valid/*.yaml"), *FLOWS.glob("edit/*.yaml"), *FLOWS.glob("warnings/*.yaml")]
)


@pytest.fixture
def hand(tmp_path: Path) -> Path:
    path = tmp_path / "hand.yaml"
    path.write_text(HAND)
    return path


def changes(before: str, after: str) -> list[str]:
    """Changed lines as '-old' / '+new', without context."""
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_given_any_flow_when_saved_without_changes_then_it_is_byte_identical(path: Path) -> None:
    ed = edit.open_flow(path)
    assert edit.render(ed) == path.read_text()


def test_given_set_when_saved_then_only_that_line_changes(hand: Path) -> None:
    # When
    ed = edit.open_flow(hand)
    edit.set_field(ed, "plan", "timeout", "20m")
    edit.set_field(ed, "build", "run", 'echo "$PLAN" && make')
    edit.save(ed)

    # Then
    assert changes(HAND, hand.read_text()) == [
        "+    timeout: 20m",
        "-    run: 'echo \"$PLAN\"'",
        "+    run: 'echo \"$PLAN\" && make'",
    ]
    assert "    next: build            # go build\n    timeout: 20m\n\n  build:" in hand.read_text()


def test_given_rename_when_saved_then_every_reference_changes_and_plain_text_does_not(
    hand: Path,
) -> None:
    # When
    ed = edit.open_flow(hand)
    edit.rename_node(ed, "plan", "planner")
    edit.save(ed)

    # Then
    text = hand.read_text()
    assert changes(HAND, text) == [
        "-  plan:",
        "+  planner:",
        '-    env: { PLAN: "${{ nodes.plan.text }}" }',
        '+    env: {PLAN: "${{ nodes.planner.text }}"}',
        '-      - when: nodes.build.exit_code == 0 and nodes["plan"].outcome == "succeeded"',
        '+      - when: nodes.build.exit_code == 0 and nodes["planner"].outcome == "succeeded"',
        "-      - when: visits.plan < 3",
        "-        to: plan",
        "+      - when: visits.planner < 3",
        "+        to: planner",
        '-        reason: "gave up on ${{ nodes.plan.text }}"',
        '+        reason: "gave up on ${{ nodes.planner.text }}"',
        "-  summary: ${{ nodes.plan.text }}",
        "+  summary: ${{ nodes.planner.text }}",
    ]
    assert "The plan node's output feeds the build." in text
    assert "  # Planning step\n  planner:" in text


def test_given_new_node_when_added_after_another_then_it_is_inserted_there(hand: Path) -> None:
    ed = edit.open_flow(hand)
    edit.add_node(ed, "lint", "shell", after="build", fields={"run": "make lint"})
    report = edit.save(ed)
    assert changes(HAND, hand.read_text()) == [
        "+  lint:",
        "+    type: shell",
        "+    run: make lint",
    ]
    assert [p.code for p in report.problems if p.code.startswith("W-")] == ["W-UNREACHABLE"]


def test_given_node_with_incoming_edges_when_removed_then_it_is_refused_unless_edges_go_too(
    hand: Path,
) -> None:
    # Given
    ed = edit.open_flow(hand)

    # When / Then
    with pytest.raises(edit.EditError, match=r"routed to from build\.next"):
        edit.remove_node(ed, "check")
    edit.remove_node(ed, "check", remove_edges=True)
    edit.save(ed)
    text = hand.read_text()
    assert "check" not in text
    assert "visits.plan < 3" in text


def test_given_edges_when_connected_and_disconnected_then_cases_follow(hand: Path) -> None:
    ed = edit.open_flow(hand)
    edit.connect(ed, "check", "plan", when="visits.check < 2")
    edit.disconnect(ed, "build", "fail")
    edit.save(ed)
    assert changes(HAND, hand.read_text()) == [
        "-      - to: fail",
        '-        reason: "gave up on ${{ nodes.plan.text }}"',
        "+      - when: visits.check < 2",
        "+        to: plan",
    ]


def test_given_cases_when_reordered_or_type_changed_then_the_tree_follows(hand: Path) -> None:
    ed = edit.open_flow(hand)
    edit.reorder_cases(ed, "check", [1, 0])
    with pytest.raises(edit.EditError, match="run"):
        edit.change_type(ed, "build", "sleep")
    dropped = edit.change_type(ed, "build", "set", drop=True)
    assert sorted(dropped) == ["env", "run"]


def test_given_edit_that_breaks_the_flow_when_saved_then_it_is_refused(hand: Path) -> None:
    ed = edit.open_flow(hand)
    edit.set_field(ed, "plan", "next", "nowhere")
    with pytest.raises(edit.EditError) as excinfo:
        edit.save(ed)
    assert [p.code for p in excinfo.value.problems] == ["E-UNKNOWN-TARGET"]
    assert hand.read_text() == HAND


def test_given_file_changed_on_disk_when_saved_then_the_edit_is_refused(hand: Path) -> None:
    ed = edit.open_flow(hand)
    edit.set_field(ed, "plan", "timeout", "5m")
    hand.write_text(HAND + "\n# someone else edited this\n")
    with pytest.raises(edit.EditConflict):
        edit.save(ed)
    assert hand.read_text().endswith("# someone else edited this\n")


def test_given_executable_file_when_edited_then_its_mode_is_kept(hand: Path) -> None:
    os.chmod(hand, 0o750)
    ed = edit.open_flow(hand)
    edit.set_field(ed, "plan", "timeout", "5m")
    edit.save(ed)
    assert stat.S_IMODE(hand.stat().st_mode) == 0o750


def test_given_cli_when_editing_then_exit_codes_follow_the_outcome(
    hand: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["flow", "rename-node", str(hand), "plan", "planner", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main(["flow", "set", str(hand), "build", "next", "nowhere"]) == 3
    assert cli.main(["flow", "unset", str(hand), "ghost", "run"]) == 2
    assert cli.main(["flow", "add-node", str(hand), "x", "--type", "shell", "--set", "run=ls"]) == 0
    assert "  x:\n    type: shell\n    run: ls\n" in hand.read_text()


def test_given_type_change_to_a_type_with_required_keys_then_its_starter_fills_them(
    hand: Path,
) -> None:
    # When: a condition becomes a sleep, which needs a duration
    ed = edit.open_flow(hand)
    edit.change_type(ed, "check", "sleep")
    edit.save(ed)

    # Then: the flow stays valid, and only the changed keys move
    assert changes(HAND, hand.read_text()) == [
        "-    type: condition",
        "+    type: sleep",
        "+    duration: 1m",
    ]


STEPS = """name: steps
nodes:
  ask:
    type: human
    message_file: ask.md
    choices: [go]
    next: say
  say:
    type: shell
    args: [echo, hi]
"""


def test_given_type_change_when_a_one_of_group_is_already_met_then_no_starter_is_added(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "steps.yaml"
    path.write_text(STEPS)
    (tmp_path / "ask.md").write_text("Go?")

    # When: `message_file` given for a human answers its `message` starter
    ed = edit.open_flow(path)
    edit.change_type(ed, "ask", "notify", drop=True)
    edit.change_type(ed, "say", "human", drop=True, fields={"message_file": "ask.md"})
    edit.save(ed)

    # Then
    assert path.read_text() == (
        "name: steps\n"
        "nodes:\n"
        "  ask:\n"
        "    type: notify\n"
        "    next: say\n"
        "    message: Done.\n"
        "    command: 'true'\n"
        "  say:\n"
        "    type: human\n"
        "    ack: true\n"
        "    message_file: ask.md\n"
    )


def test_given_type_change_with_values_for_required_keys_then_they_are_set(
    tmp_path: Path,
) -> None:
    # Given: a subflow needs a flow file, which no starter can provide
    path = tmp_path / "steps.yaml"
    path.write_text(STEPS)
    (tmp_path / "ask.md").write_text("Go?")
    (tmp_path / "child.yaml").write_text("name: child\nnodes:\n  a: {type: set, vars: {x: 1}}\n")
    ed = edit.open_flow(path)
    edit.change_type(ed, "say", "subflow", drop=True)
    with pytest.raises(edit.EditError):
        edit.save(ed)

    # When
    ed = edit.open_flow(path)
    edit.change_type(ed, "say", "subflow", drop=True, fields={"flow": "child.yaml"})
    edit.save(ed)

    # Then
    assert path.read_text().endswith("  say:\n    type: subflow\n    flow: child.yaml\n")
