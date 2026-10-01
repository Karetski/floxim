"""`${{ }}` templates in string fields."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from whisperwind.expr import Clock, ExprError, Expression, parse, render_text

OPEN = "${{"
CLOSE = "}}"
_JINJA_LIKE = re.compile(r"(?<!\$)\{\{")


@dataclass(frozen=True)
class Part:
    """A literal piece of text, or an expression with its offset in the template."""

    text: str
    expression: Expression | None = None
    offset: int = 0


@dataclass(frozen=True)
class Template:
    source: str
    parts: tuple[Part, ...]

    @property
    def expressions(self) -> list[Part]:
        return [p for p in self.parts if p.expression is not None]

    @property
    def is_whole_value(self) -> bool:
        """The whole field is one `${{ }}`: the result keeps its type."""
        return len(self.parts) == 1 and self.parts[0].expression is not None

    def render(self, namespace: Mapping[str, Any], clock: Clock | None = None) -> Any:
        if self.is_whole_value:
            expression = self.parts[0].expression
            assert expression is not None
            return expression.evaluate(namespace, clock)
        out = []
        for part in self.parts:
            if part.expression is None:
                out.append(part.text)
            else:
                out.append(render_text(part.expression.evaluate(namespace, clock)))
        return "".join(out)


def parse_template(source: str) -> Template:
    """Split a string into text and expressions. Raises ExprError; its offset is
    relative to the start of `source`."""
    parts: list[Part] = []
    text: list[str] = []
    i = 0
    while i < len(source):
        if source.startswith("$" + OPEN, i):
            text.append(OPEN)
            i += len(OPEN) + 1
            continue
        if not source.startswith(OPEN, i):
            text.append(source[i])
            i += 1
            continue
        start = i + len(OPEN)
        expression, end = _parse_until_close(source, start)
        if text:
            parts.append(Part("".join(text)))
            text = []
        parts.append(Part(source[i:end], expression, start))
        i = end
    if text:
        parts.append(Part("".join(text)))
    return Template(source, tuple(parts))


def _parse_until_close(source: str, start: int) -> tuple[Expression, int]:
    """Parse from `start` up to the first `}}` after which the expression parses."""
    search = start
    first_error: ExprError | None = None
    while True:
        close = source.find(CLOSE, search)
        if close == -1:
            if first_error is not None:
                raise first_error
            raise ExprError("E-EXPR-SYNTAX", "`${{` without a closing `}}`", start - len(OPEN))
        try:
            return parse(source[start:close]), close + len(CLOSE)
        except ExprError as exc:
            if exc.code != "E-EXPR-SYNTAX":
                exc.offset += start
                raise
            if first_error is None:
                exc.offset += start
                first_error = exc
        search = close + 1


def has_template(value: Any) -> bool:
    return isinstance(value, str) and OPEN in value.replace("$" + OPEN, "")


def jinja_like_offsets(source: str) -> list[int]:
    """Offsets of `{{` not preceded by `$` outside expressions (W-JINJA-LIKE)."""
    try:
        template = parse_template(source)
    except ExprError:
        return []
    offsets = []
    position = 0
    for part in template.parts:
        if part.expression is None:
            offsets += [position + m.start() for m in _JINJA_LIKE.finditer(part.text)]
            position += len(part.text)
        else:
            position += len(part.text)
    return offsets


def render_value(value: Any, namespace: Mapping[str, Any], clock: Clock | None = None) -> Any:
    """Render every template inside a (possibly nested) configuration value."""
    if isinstance(value, str):
        return parse_template(value).render(namespace, clock) if OPEN in value else value
    if isinstance(value, list):
        return [render_value(v, namespace, clock) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, namespace, clock) for k, v in value.items()}
    return value
