"""Masking secrets in everything Floxim writes itself (spec §12.4)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MASK = "[REDACTED]"


class Redactor:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = [re.compile(p) for p in patterns]

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def text(self, value: str) -> str:
        for pattern in self.patterns:
            value = pattern.sub(MASK, value)
        return value

    def value(self, value: Any) -> Any:
        """Redact every string inside a JSON value."""
        if not self.patterns:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value
