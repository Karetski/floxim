"""Structured edits of a flow file (spec §10.4).

Every edit, from the TUI or from `floxim flow <op>`, goes through this module:

1. Read the file in ruamel's round-trip mode and record its SHA-256.
2. Apply the operation to the round-trip tree.
3. Render the result so that only changed lines change: both the original and
   the edited tree are dumped in the same normalized style, the two dumps are
   diffed, and only the changed hunks are applied to the original text. Lines an
   edit does not touch stay byte-identical, comments and quoting included.
4. Validate the result; an edit that makes a valid file invalid is refused.
5. Re-read the file; if it changed since step 1, refuse rather than overwrite.
6. Write atomically, keeping the file's mode.
"""

from __future__ import annotations

import ast
import dataclasses
import difflib
import hashlib
import io
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.scalarstring import ScalarString
from ruamel.yaml.tokens import CommentToken
from ruamel.yaml.util import load_yaml_guess_indent

from floxim.expr import ExprError
from floxim.expr import parse as parse_expression
from floxim.flowspec import EXACTLY_ONE, NODE_TYPES, node_fields
from floxim.problems import Problem
from floxim.schema import IDENTIFIER
from floxim.templates import OPEN, parse_template
from floxim.validate import Report, validate

ROUTING = ("next", "on_error")


class EditError(Exception):
    """The edit is refused; the file is unchanged."""

    def __init__(self, message: str, problems: list[Problem] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class EditConflict(Exception):
    """The file changed on disk since it was read."""


@dataclass
class Editable:
    path: Path
    text: str
    sha256: str
    tree: CommentedMap
    yaml: YAML
    baseline: str  # the unedited tree in the normalized style

    @property
    def nodes(self) -> CommentedMap:
        nodes = self.tree.get("nodes")
        if not isinstance(nodes, CommentedMap):
            raise EditError("the flow has no nodes mapping")
        return nodes

    def node(self, node_id: str) -> CommentedMap:
        node = self.nodes.get(node_id)
        if not isinstance(node, CommentedMap):
            raise EditError(f"no node named {node_id!r}")
        return node


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _yaml_for(text: str) -> YAML:
    _, sequence, offset = load_yaml_guess_indent(text)
    indents = [len(i) for i in re.findall(r"^( +)[A-Za-z_][\w-]*:", text, re.M)]
    mapping = min(indents) if indents else 2
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=mapping, sequence=sequence or mapping, offset=offset or 0)
    return yaml


def _dump(yaml: YAML, tree: Any) -> str:
    out = io.StringIO()
    yaml.dump(tree, out)
    return out.getvalue()


def open_flow(path: Path) -> Editable:
    text = path.read_text(encoding="utf-8")
    yaml = _yaml_for(text)
    tree = yaml.load(text)
    if not isinstance(tree, CommentedMap):
        raise EditError("a flow file must be a mapping")
    return Editable(path, text, _sha(text), tree, yaml, _dump(yaml, tree))


def render(ed: Editable) -> str:
    """The edited file: the original text with only the changed hunks replaced."""
    edited = _dump(ed.yaml, ed.tree)
    original = ed.text.splitlines(keepends=True)
    before = ed.baseline.splitlines(keepends=True)
    after = edited.splitlines(keepends=True)
    if len(before) != len(original):
        return edited  # formatting too unusual to line up; fall back to the dump
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        out += original[i1:i2] if tag == "equal" else after[j1:j2]
    return "".join(out)


def save(ed: Editable, *, allow_invalid: bool = False) -> Report:
    """Validate and write the edited flow (steps 3–6). Raises EditError or EditConflict."""
    text = render(ed)
    was_valid = _validate_text(ed.path, ed.text).ok()
    report = _validate_text(ed.path, text)
    if was_valid and not report.ok() and not allow_invalid:
        raise EditError("the edit would make the flow invalid", report.errors)
    current = ed.path.read_text(encoding="utf-8")
    if _sha(current) != ed.sha256:
        raise EditConflict(f"{ed.path} changed on disk since it was read; reload and try again")
    mode = ed.path.stat().st_mode & 0o7777
    fd, temp = tempfile.mkstemp(dir=ed.path.parent, prefix=f".{ed.path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, ed.path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    ed.text, ed.sha256, ed.baseline = text, _sha(text), _dump(ed.yaml, ed.tree)
    return report


def _validate_text(path: Path, text: str) -> Report:
    """Validate `text` as if it were `path` (relative references still resolve)."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.check.", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        report = validate(Path(temp), workdir=path.parent, implementation_gate=False)
    finally:
        Path(temp).unlink(missing_ok=True)
    report.problems = [
        dataclasses.replace(p, file=str(path)) if p.file == temp else p for p in report.problems
    ]
    report.path = path
    return report


# -- helpers ----------------------------------------------------------------------------


def _tail_owner(node: Any) -> tuple[CommentedMap, Any] | None:
    """The mapping key whose comment token holds the blank lines after `node`:
    ruamel keeps them on the deepest last key."""
    if isinstance(node, CommentedMap) and node:
        key = list(node)[-1]
        return _tail_owner(node[key]) or (node, key)
    if isinstance(node, CommentedSeq) and node:
        return _tail_owner(node[-1])
    return None


def _take_blank(node: Any) -> str:
    owner = _tail_owner(node)
    if owner is None:
        return ""
    mapping, key = owner
    tokens = mapping.ca.items.get(key)
    token = tokens[2] if tokens and len(tokens) > 2 else None
    if token is None:
        return ""
    value = str(token.value)
    body = value.rstrip("\n")
    blank = value[len(body) :][1:]  # newlines after the line's own end
    if not blank:
        return ""
    if body:
        token.value = body + "\n"
    else:
        tokens[2] = None
    return blank


def _give_blank(node: Any, blank: str) -> None:
    owner = _tail_owner(node)
    if owner is None or not blank:
        return
    mapping, key = owner
    tokens = mapping.ca.items.setdefault(key, [None, None, None, None])
    while len(tokens) < 4:
        tokens.append(None)
    if tokens[2] is not None:
        tokens[2].value = str(tokens[2].value).rstrip("\n") + "\n" + blank
    else:
        tokens[2] = CommentToken("\n" + blank, CommentMark(0), None)


@contextmanager
def _keeping_layout(scope: Any) -> Iterator[None]:
    """Keep the blank lines that follow `scope` after it, whatever the edit changes
    at its end."""
    blank = _take_blank(scope)
    try:
        yield
    finally:
        _give_blank(scope, blank)


def _like(old: Any, new: Any) -> Any:
    """`new`, in the scalar style `old` was written in."""
    if isinstance(old, ScalarString) and isinstance(new, str):
        return type(old)(new)
    return new


def _check_id(node_id: str) -> None:
    if not re.match(IDENTIFIER, node_id):
        raise EditError(f"{node_id!r} is not a valid node ID")


def _cases(node: CommentedMap, key: str) -> Any:
    return node.get(key)


def routes_to(ed: Editable, target: str) -> list[tuple[str, str]]:
    """(node, key) pairs whose next/on_error names `target`."""
    found = []
    for node_id, node in ed.nodes.items():
        if not isinstance(node, CommentedMap):
            continue
        for key in ROUTING:
            value = node.get(key)
            if value == target or (
                isinstance(value, list)
                and any(isinstance(c, dict) and c.get("to") == target for c in value)
            ):
                found.append((str(node_id), key))
    return found


# -- operations --------------------------------------------------------------------------

STARTERS: dict[str, dict[str, Any]] = {
    "agent": {"prompt": "Describe the task."},
    "shell": {"run": "true"},
    "condition": {"next": [{"to": "end"}]},
    "human": {"message": "Continue?", "ack": True},
    "sleep": {"duration": "1m"},
    "set": {"vars": {}},
    "notify": {"message": "Done.", "command": "true"},
}


def add_node(
    ed: Editable,
    node_id: str,
    node_type: str,
    *,
    after: str | None = None,
    fields: dict[str, Any] | None = None,
) -> None:
    """Insert a node after `after` (default: last), with a starter body for its type."""
    _check_id(node_id)
    if node_type not in NODE_TYPES:
        raise EditError(f"unknown node type {node_type!r}")
    nodes = ed.nodes
    if node_id in nodes:
        raise EditError(f"a node named {node_id!r} already exists")
    body = CommentedMap()
    body["type"] = node_type
    for key, value in {**STARTERS.get(node_type, {}), **(fields or {})}.items():
        body[key] = value
    keys = list(nodes)
    if after is not None and after not in keys:
        raise EditError(f"no node named {after!r}")
    position = keys.index(after) + 1 if after is not None else len(keys)
    scope = nodes[keys[position - 1]] if 0 < position < len(keys) else nodes
    with _keeping_layout(scope):
        nodes.insert(position, node_id, body)


def remove_node(ed: Editable, node_id: str, *, remove_edges: bool = False) -> None:
    """Remove a node. Refused while other nodes route to it, unless `remove_edges`."""
    ed.node(node_id)
    incoming = [(n, k) for n, k in routes_to(ed, node_id) if n != node_id]
    if incoming and not remove_edges:
        names = ", ".join(f"{n}.{k}" for n, k in incoming)
        raise EditError(f"{node_id!r} is routed to from {names}; remove those edges first")
    for source, key in incoming:
        disconnect(ed, source, node_id, on_error=key == "on_error")
    if ed.tree.get("start") == node_id:
        del ed.tree["start"]
    keys = list(ed.nodes)
    index = keys.index(node_id)
    blank = _take_blank(ed.nodes[node_id])
    del ed.nodes[node_id]
    if index > 0:
        _give_blank(ed.nodes[keys[index - 1]], blank)


def set_field(ed: Editable, node_id: str, path: str, value: Any) -> None:
    """Set `path` (dotted) in a node, keeping the existing scalar style."""
    node = ed.node(node_id)
    with _keeping_layout(node):
        target, key = _walk(node, path, create=True)
        target[key] = _like(target.get(key), value)


def unset_field(ed: Editable, node_id: str, path: str) -> None:
    node = ed.node(node_id)
    target, key = _walk(node, path, create=False)
    if key not in target:
        raise EditError(f"{node_id} has no {path}")
    with _keeping_layout(node):
        del target[key]


def _walk(node: CommentedMap, path: str, *, create: bool) -> tuple[CommentedMap, str]:
    parts = path.split(".")
    current = node
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, CommentedMap):
            if not create:
                raise EditError(f"no {path}")
            child = CommentedMap()
            current[part] = child
        current = child
    return current, parts[-1]


def connect(
    ed: Editable,
    source: str,
    target: str,
    *,
    when: str | None = None,
    on_error: bool = False,
    position: int | None = None,
) -> None:
    """Add an edge. Without `when`, it becomes the default route; with `when`, a case
    before the default (or at `position`)."""
    node = ed.node(source)
    if target not in ("end", "fail") and target not in ed.nodes:
        raise EditError(f"no node named {target!r}")
    with _keeping_layout(node):
        _connect(node, target, when, on_error, position)


def _connect(
    node: CommentedMap, target: str, when: str | None, on_error: bool, position: int | None
) -> None:
    key = "on_error" if on_error else "next"
    current = node.get(key)
    if when is not None:
        try:
            parse_expression(when)
        except ExprError as exc:
            raise EditError(f"when: {exc.message}") from None
    if when is None:
        if isinstance(current, list):
            default = next((c for c in current if isinstance(c, dict) and "when" not in c), None)
            if default is not None:
                default["to"] = _like(default.get("to"), target)
            else:
                current.append(_case(None, target))
        else:
            node[key] = _like(current, target)
        return
    case = _case(when, target)
    if isinstance(current, list):
        cases = current
    else:
        fallback = current if isinstance(current, str) else ("end" if key == "next" else "fail")
        cases = CommentedSeq([_case(None, fallback)])
        node[key] = cases
    defaults = [i for i, c in enumerate(cases) if isinstance(c, dict) and "when" not in c]
    index = position if position is not None else (defaults[0] if defaults else len(cases))
    cases.insert(index, case)


def _case(when: str | None, target: str) -> CommentedMap:
    case = CommentedMap()
    if when is not None:
        case["when"] = when
    case["to"] = target
    return case


def disconnect(ed: Editable, source: str, target: str, *, on_error: bool = False) -> None:
    node = ed.node(source)
    with _keeping_layout(node):
        _disconnect(node, source, target, on_error)


def _disconnect(node: CommentedMap, source: str, target: str, on_error: bool) -> None:
    key = "on_error" if on_error else "next"
    current = node.get(key)
    if current == target:
        del node[key]
        return
    if not isinstance(current, list):
        raise EditError(f"{source}.{key} does not route to {target!r}")
    kept = [c for c in current if not (isinstance(c, dict) and c.get("to") == target)]
    if len(kept) == len(current):
        raise EditError(f"{source}.{key} does not route to {target!r}")
    if not kept:
        del node[key]
        return
    for case in [c for c in current if c not in kept]:
        current.remove(case)


def reorder_cases(ed: Editable, node_id: str, order: list[int], *, on_error: bool = False) -> None:
    node = ed.node(node_id)
    key = "on_error" if on_error else "next"
    cases = node.get(key)
    if not isinstance(cases, list) or sorted(order) != list(range(len(cases))):
        raise EditError(f"order must be a permutation of {key}'s {len(cases or [])} cases")
    with _keeping_layout(node):
        reordered = [cases[i] for i in order]
        for index, case in enumerate(reordered):
            cases[index] = case


def change_type(
    ed: Editable,
    node_id: str,
    new_type: str,
    *,
    drop: bool = False,
    fields: dict[str, Any] | None = None,
) -> list[str]:
    """Change a node's type. Keys the new type does not accept must be dropped
    explicitly; keys it needs come from its starter body, unless the node already
    has them, and from `fields`. Returns the dropped keys."""
    if new_type not in NODE_TYPES:
        raise EditError(f"unknown node type {new_type!r}")
    node = ed.node(node_id)
    accepted = set(node_fields(new_type))
    extra = [k for k in node if k not in accepted and not str(k).startswith("x-")]
    if extra and not drop:
        raise EditError(f"{new_type} nodes have no {', '.join(extra)}; confirm to drop them")
    with _keeping_layout(node):
        for key in extra:
            del node[key]
        node["type"] = _like(node.get("type"), new_type)
        given = fields or {}
        for key, value in {**starter(new_type, set(node) | set(given)), **given}.items():
            node[key] = value
    return extra


def starter(node_type: str, present: set[str]) -> dict[str, Any]:
    """The starter keys a node of `node_type` still needs, given the keys it has.
    A key is skipped when another key of its one-of group is present."""
    taken = {k for group in EXACTLY_ONE.get(node_type, ()) if present & set(group) for k in group}
    return {k: v for k, v in STARTERS.get(node_type, {}).items() if k not in present | taken}


# -- rename -------------------------------------------------------------------------------


def rename_node(ed: Editable, old: str, new: str) -> None:
    """Rename a node and every reference to it: routing targets, `start`, session
    sources, handoff `from`, and expressions and templates (through their AST, so
    `nodes.old.x`, `visits.old` and `nodes["old"]` change and plain text does not)."""
    _check_id(new)
    nodes = ed.nodes
    ed.node(old)
    if new in nodes:
        raise EditError(f"a node named {new!r} already exists")
    position = list(nodes).index(old)
    comment = nodes.ca.items.pop(old, None)
    body = nodes.pop(old)
    nodes.insert(position, new, body)
    if comment is not None:
        nodes.ca.items[new] = comment
    if ed.tree.get("start") == old:
        ed.tree["start"] = _like(ed.tree["start"], new)
    for node in nodes.values():
        if not isinstance(node, CommentedMap):
            continue
        for key in ROUTING:
            value = node.get(key)
            if value == old:
                node[key] = _like(value, new)
            elif isinstance(value, list):
                for case in value:
                    if isinstance(case, dict) and case.get("to") == old:
                        case["to"] = _like(case["to"], new)
        session = node.get("session")
        if isinstance(session, dict):
            for key in ("resume", "fork"):
                if session.get(key) == old:
                    session[key] = _like(session[key], new)
        if node.get("type") == "handoff" and node.get("from") == old:
            node["from"] = _like(node["from"], new)
    _rewrite_strings(ed.tree, lambda text, is_expression: _rename_in(text, old, new, is_expression))


def _rewrite_strings(tree: Any, rewrite: Callable[[str, bool], str]) -> None:
    for container, key, value, is_expression in _strings(tree):
        updated = rewrite(value, is_expression)
        if updated != value:
            container[key] = _like(value, updated)


def _strings(value: Any) -> Iterator[tuple[Any, Any, str, bool]]:
    """Every string in the tree: (container, key, text, is a `when` expression)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield value, key, item, key == "when"
            else:
                yield from _strings(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                yield value, index, item, False
            else:
                yield from _strings(item)


def _rename_in(text: str, old: str, new: str, is_expression: bool) -> str:
    if is_expression:
        return _rename_expression(text, old, new)
    if OPEN not in text:
        return text
    try:
        template = parse_template(text)
    except ExprError:
        return text
    out = text
    for part in reversed(template.expressions):
        assert part.expression is not None
        end = part.offset + len(part.expression.text)
        out = out[: part.offset] + _rename_expression(part.expression.text, old, new) + out[end:]
    return out


def _rename_expression(text: str, old: str, new: str) -> str:
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return text
    lead = len(text) - len(text.lstrip())
    lines = text.strip().splitlines(keepends=True)
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line))

    def offset(line: int, col: int) -> int:
        """A character offset into `text` from ast's line and UTF-8 byte column."""
        prefix = lines[line - 1].encode("utf-8")[:col].decode("utf-8", errors="ignore")
        return lead + line_starts[line - 1] + len(prefix)

    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == old
            and isinstance(node.value, ast.Name)
            and node.value.id in ("nodes", "visits")
            and node.end_lineno is not None
            and node.end_col_offset is not None
        ):
            end = offset(node.end_lineno, node.end_col_offset)
            spans.append((end - len(old), end, new))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("nodes", "visits")
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == old
            and node.slice.end_col_offset is not None
            and node.slice.end_lineno is not None
        ):
            start = offset(node.slice.lineno, node.slice.col_offset)
            end = offset(node.slice.end_lineno, node.slice.end_col_offset)
            quote = text[start]
            spans.append((start, end, f"{quote}{new}{quote}"))
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text
