"""Reading flow YAML into plain data with source positions."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.events import AliasEvent, NodeEvent, ScalarEvent

from whisperwind.problems import Problem, join_pointer

Position = tuple[int, int]  # 1-based line and column


@dataclass
class Document:
    """Plain JSON-like data plus the source position of every value and key."""

    file: str
    data: Any
    values: dict[str, Position] = field(default_factory=dict)
    keys: dict[str, Position] = field(default_factory=dict)

    def position(self, pointer: str, *, key: bool = False) -> Position | None:
        """Position of the value (or key) at `pointer`, else of its nearest ancestor."""
        table = self.keys if key else self.values
        while True:
            if pointer in table:
                return table[pointer]
            if pointer in self.values:
                return self.values[pointer]
            if not pointer:
                return None
            pointer = pointer.rsplit("/", 1)[0]

    def problem(
        self, code: str, message: str, pointer: str, *, key: bool = False, hint: str | None = None
    ) -> Problem:
        pos = self.position(pointer, key=key)
        return Problem(
            code,
            message,
            file=self.file,
            line=pos[0] if pos else None,
            column=pos[1] if pos else None,
            pointer=pointer,
            hint=hint,
        )


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.version = (1, 2)
    return y


def _plain(node: Any, pointer: str, doc: Document) -> Any:
    if isinstance(node, CommentedMap):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child = join_pointer(pointer, str(key))
            kline, kcol = node.lc.key(key)
            vline, vcol = node.lc.value(key)
            doc.keys[child] = (kline + 1, kcol + 1)
            doc.values[child] = (vline + 1, vcol + 1)
            out[str(key)] = _plain(value, child, doc)
        return out
    if isinstance(node, CommentedSeq):
        items: list[Any] = []
        for index, value in enumerate(node):
            child = join_pointer(pointer, index)
            line, col = node.lc.item(index)
            doc.values[child] = (line + 1, col + 1)
            items.append(_plain(value, child, doc))
        return items
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, (datetime.date, datetime.datetime)):
        return node.isoformat()  # YAML 1.2 core has no timestamps
    return str(node)


def _alias_problems(text: str, file: str) -> list[Problem]:
    problems: list[Problem] = []
    for event in _yaml().parse(text):
        uses_anchor = isinstance(event, AliasEvent) or (
            isinstance(event, NodeEvent) and event.anchor is not None
        )
        is_merge = isinstance(event, ScalarEvent) and event.value == "<<" and event.style is None
        if uses_anchor or is_merge:
            mark = event.start_mark
            problems.append(
                Problem(
                    "E-YAML-ALIAS",
                    "anchors, aliases and merge keys are not allowed; use templates instead",
                    file=file,
                    line=mark.line + 1,
                    column=mark.column + 1,
                )
            )
    return problems


def load_text(text: str, file: str) -> tuple[Document | None, list[Problem]]:
    try:
        problems = _alias_problems(text, file)
        if problems:
            return None, problems
        raw = _yaml().load(text)
    except DuplicateKeyError as exc:
        return None, [_marked("E-DUPLICATE-KEY", "duplicate mapping key", exc, file)]
    except MarkedYAMLError as exc:
        return None, [_marked("E-YAML", str(exc.problem or exc), exc, file)]
    except YAMLError as exc:
        return None, [Problem("E-YAML", str(exc), file=file)]
    doc = Document(file=file, data=None)
    doc.values[""] = (1, 1)
    doc.data = _plain(raw, "", doc)
    return doc, []


def load_file(path: Path) -> tuple[Document | None, list[Problem]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, [Problem("E-FILE-NOT-FOUND", f"file not found: {path}", file=str(path))]
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Problem("E-YAML", f"cannot read file: {exc}", file=str(path))]
    return load_text(text, str(path))


def _marked(code: str, message: str, exc: MarkedYAMLError, file: str) -> Problem:
    mark = exc.problem_mark or exc.context_mark
    return Problem(
        code,
        message,
        file=file,
        line=mark.line + 1 if mark else None,
        column=mark.column + 1 if mark else None,
    )
