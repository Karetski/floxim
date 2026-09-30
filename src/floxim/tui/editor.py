"""Editing a flow from the TUI.

Every change goes through `floxim.edit`, exactly like `floxim flow <op>`, and
reaches the screen through the same file polling as an edit made by hand or by
an agent.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from floxim import edit
from floxim.flowspec import COMMON, NODE_TYPES, REQUIRED_KEYS, node_fields

Operation = Callable[[edit.Editable], None]


@dataclass
class Failure:
    """Why an edit was not written. `kind` is "conflict" (the file changed since
    the form was opened), "invalid" (the edit would make a valid flow invalid;
    the free-text field editor may still save it as a draft) or "refused"."""

    kind: str
    message: str


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(
    path: Path, operation: Operation, *, based_on: str | None = None, allow_invalid: bool = False
) -> Failure | None:
    """Run one edit end to end, exactly as `floxim flow <op>` does. `based_on` is
    the file's SHA-256 when the person started the edit."""
    try:
        editable = edit.open_flow(path)
        if based_on is not None and editable.sha256 != based_on:
            return Failure("conflict", f"{path.name} changed on disk while you were editing")
        operation(editable)
        edit.save(editable, allow_invalid=allow_invalid)
    except edit.EditConflict as exc:
        return Failure("conflict", str(exc))
    except edit.EditError as exc:
        detail = f": {exc.problems[0].render()}" if exc.problems else ""
        return Failure("invalid" if exc.problems else "refused", f"{exc}{detail}")
    return None


def _blocks(value: Any) -> Any:
    """Multi-line strings as `|` blocks, the way prompts are usually written."""
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value)
    if isinstance(value, dict):
        return {k: _blocks(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_blocks(v) for v in value]
    return value


def yaml_text(value: Any) -> str:
    """A value as block-style YAML, keys in their written order."""
    if value is None:
        return ""
    yaml = YAML(typ="rt", pure=True)
    yaml.width = 4096
    out = io.StringIO()
    yaml.dump(_blocks(value), out)
    text = out.getvalue()
    return text[:-5] if text.endswith("\n...\n") else text.rstrip("\n")


def parse_yaml(text: str) -> Any:
    return YAML(typ="safe", pure=True).load(text)


# What to show in an empty input for a required key no starter body can fill.
PLACEHOLDERS = {
    "harness": "harness, e.g. claude (leave empty if defaults set it)",
    "call": "module:function",
    "flow": "path/to/flow.yaml, relative to this file",
    "items": "${{ nodes.<id>.output }}",
    "from": "the agent node whose session to hand off",
}


def needed(node_type: str, present: set[str]) -> list[str]:
    """Required keys of `node_type` that neither the node nor its starter body has."""
    starter = edit.STARTERS.get(node_type, {})
    return [k for k in REQUIRED_KEYS.get(node_type, ()) if k not in starter and k not in present]


class RequiredFields(Vertical):
    """Inputs for the keys a node type needs a value for, rebuilt when the type changes."""

    DEFAULT_CSS = "RequiredFields { height: auto; }"

    def show(self, node_type: str, present: set[str]) -> None:
        self.remove_children()
        self.mount_all(
            Input(placeholder=f"{key}: {PLACEHOLDERS.get(key, '')}", id=f"field-{key}")
            for key in needed(node_type, present)
        )

    def values(self) -> dict[str, str]:
        found = {}
        for widget in self.query(Input):
            if widget.value.strip() and widget.id:
                found[widget.id.removeprefix("field-")] = widget.value.strip()
        return found


class _Modal(ModalScreen[Operation | None]):
    DEFAULT_CSS = """
    _Modal { align: center middle; }
    #box { width: 80%; height: auto; max-height: 90%; border: thick $accent; background: $surface; }
    #box > * { margin: 0 1; }
    #error { color: $error; height: auto; }
    TextArea { height: 12; }
    """

    heading = ""

    def body(self) -> ComposeResult:
        yield from ()

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(Text(self.heading, style="bold"))
            yield from self.body()
            with Horizontal():
                yield Button("OK", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")
            yield Static("", id="error")

    def operation(self) -> Operation:
        raise NotImplementedError

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        try:
            self.dismiss(self.operation())
        except ValueError as exc:
            self.query_one("#error", Static).update(Text(str(exc)))


class AddNodeModal(_Modal):
    heading = "Add node"

    def __init__(self, after: str | None) -> None:
        super().__init__()
        self.after = after

    def body(self) -> ComposeResult:
        yield Input(placeholder="node ID", id="node-id")
        yield Select([(t, t) for t in NODE_TYPES], value="shell", allow_blank=False, id="node-type")
        yield RequiredFields(id="required")

    @on(Select.Changed, "#node-type")
    def _type(self, event: Select.Changed) -> None:
        self.query_one(RequiredFields).show(str(event.value), set())

    def operation(self) -> Operation:
        node_id = self.query_one("#node-id", Input).value.strip()
        node_type = str(self.query_one("#node-type", Select).value)
        fields = self.query_one(RequiredFields).values()
        return lambda ed: edit.add_node(ed, node_id, node_type, after=self.after, fields=fields)


class RemoveNodeModal(_Modal):
    def __init__(self, node: str) -> None:
        super().__init__()
        self.node = node
        self.heading = f"Remove {node}?"

    def body(self) -> ComposeResult:
        yield Checkbox("also remove the edges that route to it", id="remove-edges")

    def operation(self) -> Operation:
        remove_edges = self.query_one("#remove-edges", Checkbox).value
        return lambda ed: edit.remove_node(ed, self.node, remove_edges=remove_edges)


class RenameModal(_Modal):
    def __init__(self, node: str) -> None:
        super().__init__()
        self.node = node
        self.heading = f"Rename {node}"

    def body(self) -> ComposeResult:
        yield Input(value=self.node, id="new-id")

    def operation(self) -> Operation:
        new = self.query_one("#new-id", Input).value.strip()
        return lambda ed: edit.rename_node(ed, self.node, new)


class ConnectModal(_Modal):
    def __init__(self, node: str, targets: list[str], disconnect: bool = False) -> None:
        super().__init__()
        self.node = node
        self.targets = targets
        self.disconnect = disconnect
        self.heading = f"{'Disconnect' if disconnect else 'Connect'} {node}"

    def body(self) -> ComposeResult:
        yield Select(
            [(t, t) for t in self.targets], allow_blank=False, value=self.targets[0], id="target"
        )
        if not self.disconnect:
            yield Input(placeholder="when (empty: the default route)", id="when")
        yield Checkbox("on_error instead of next", id="on-error")

    def operation(self) -> Operation:
        target = str(self.query_one("#target", Select).value)
        on_error = self.query_one("#on-error", Checkbox).value
        if self.disconnect:
            return lambda ed: edit.disconnect(ed, self.node, target, on_error=on_error)
        when = self.query_one("#when", Input).value.strip() or None
        return lambda ed: edit.connect(ed, self.node, target, when=when, on_error=on_error)


class ReorderModal(_Modal):
    def __init__(self, node: str, count: int) -> None:
        super().__init__()
        self.node = node
        self.count = count
        self.heading = f"Reorder {node}'s cases"

    def body(self) -> ComposeResult:
        example = ", ".join(str(i) for i in reversed(range(self.count)))
        yield Label(f"New order of the {self.count} cases, e.g. {example}")
        yield Input(value=", ".join(str(i) for i in range(self.count)), id="order")
        yield Checkbox("on_error instead of next", id="on-error")

    def operation(self) -> Operation:
        try:
            order = [int(p) for p in self.query_one("#order", Input).value.split(",")]
        except ValueError:
            raise ValueError("give the case numbers, separated by commas") from None
        on_error = self.query_one("#on-error", Checkbox).value
        return lambda ed: edit.reorder_cases(ed, self.node, order, on_error=on_error)


class ChangeTypeModal(_Modal):
    def __init__(self, node: str, current: str, present: set[str]) -> None:
        super().__init__()
        self.node = node
        self.current = current
        self.present = present
        self.heading = f"Change {node}'s type"

    def body(self) -> ComposeResult:
        yield Select(
            [(t, t) for t in NODE_TYPES], value=self.current, allow_blank=False, id="node-type"
        )
        yield RequiredFields(id="required")
        yield Checkbox("drop keys the new type does not accept", id="drop")

    @on(Select.Changed, "#node-type")
    def _type(self, event: Select.Changed) -> None:
        self.query_one(RequiredFields).show(str(event.value), self.present)

    def operation(self) -> Operation:
        new_type = str(self.query_one("#node-type", Select).value)
        drop = self.query_one("#drop", Checkbox).value
        fields = self.query_one(RequiredFields).values()

        def operation(ed: edit.Editable) -> None:
            edit.change_type(ed, self.node, new_type, drop=drop, fields=fields)

        return operation


class FieldModal(_Modal):
    """One field of a node, edited as YAML. The field list comes from the node
    type's declared fields; an empty value removes the field."""

    def __init__(
        self, node: str, node_type: str, config: dict[str, Any], field: str | None = None
    ) -> None:
        super().__init__()
        self.node = node
        self.config = config
        fields = [k for k in node_fields(node_type) if k not in ("type", "next", "on_error")]
        # The type's own fields first: they are what a new node usually needs.
        self.fields = sorted(fields, key=lambda k: k in COMMON)
        self.field = field if field in self.fields else self.fields[0]
        self.heading = f"Edit {node}"

    def body(self) -> ComposeResult:
        yield Select([(k, k) for k in self.fields], value=self.field, allow_blank=False, id="field")
        yield TextArea(yaml_text(self.config.get(self.field)), id="value")

    @on(Select.Changed, "#field")
    def _field(self, event: Select.Changed) -> None:
        self.field = str(event.value)
        self.query_one("#value", TextArea).text = yaml_text(self.config.get(self.field))

    def operation(self) -> Operation:
        text = self.query_one("#value", TextArea).text
        field = self.field
        if not text.strip():
            if field not in self.config:
                return lambda ed: None
            return lambda ed: edit.unset_field(ed, self.node, field)
        value = parse_yaml(text)
        return lambda ed: edit.set_field(ed, self.node, field, value)


class ConfirmModal(ModalScreen[bool]):
    """A yes/no question about an edit."""

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #confirm-box { width: 70%; height: auto; border: thick $warning; background: $surface; }
    #confirm-box > * { margin: 0 1; }
    """

    def __init__(self, question: str, yes: str) -> None:
        super().__init__()
        self.question = question
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(Text(self.question))
            with Horizontal():
                yield Button(self.yes, id="confirm-yes", variant="warning")
                yield Button("Discard my edit", id="confirm-no")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


def edit_in_editor(text: str, suffix: str = ".md") -> str | None:
    """Edit text in $VISUAL/$EDITOR; None when it was not changed."""
    from floxim.tui.actions import editor_command, interactive

    fd, name = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    try:
        interactive(editor_command(name))
        updated = Path(name).read_text()
    finally:
        Path(name).unlink(missing_ok=True)
    return None if updated == text else updated
