"""Validation problems."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEVERITIES = {"E": "error", "W": "warning", "I": "info"}


@dataclass(frozen=True)
class Problem:
    """One finding. The severity follows from the code's prefix."""

    code: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    pointer: str = ""
    hint: str | None = None

    @property
    def severity(self) -> str:
        return SEVERITIES[self.code[0]]

    @property
    def is_error(self) -> bool:
        return self.code.startswith("E-")

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "pointer": self.pointer,
            "hint": self.hint,
        }

    def render(self) -> str:
        where = self.file or "<flow>"
        if self.line is not None:
            where += f":{self.line}"
            if self.column is not None:
                where += f":{self.column}"
        path = pointer_to_path(self.pointer)
        text = f"{where} {self.severity} {self.code}  {path + ': ' if path else ''}{self.message}"
        if self.hint:
            text += f" ({self.hint})"
        return text


def escape_token(token: str | int) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def join_pointer(pointer: str, token: str | int) -> str:
    return f"{pointer}/{escape_token(token)}"


def pointer_to_path(pointer: str) -> str:
    """Render a JSON pointer as a dotted path: /nodes/test/next/1/to → nodes.test.next[1].to."""
    if not pointer:
        return ""
    out = ""
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if token.isdigit():
            out += f"[{token}]"
        else:
            out += ("." if out else "") + token
    return out
