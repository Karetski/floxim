"""Durations and sizes as written in flow files."""

from __future__ import annotations

import re
from typing import Any

_DURATION_RE = re.compile(r"^(?:\d+[smhd])+$")
_DURATION_PART = re.compile(r"(\d+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_SIZE_RE = re.compile(r"^(\d+)(B|KiB|MiB|GiB)?$")
_UNIT_BYTES = {None: 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def parse_duration(value: Any) -> float | None:
    """Seconds for a duration, None for "none". Raises ValueError when malformed."""
    if value == "none":
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("a duration cannot be negative")
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return float(text)
        if _DURATION_RE.match(text):
            return float(sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION_PART.findall(text)))
    raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 10m, 1h30m, 3d)")


def duration_error(value: Any, allow_none: bool = False) -> str | None:
    if value == "none" and not allow_none:
        return '"none" is not allowed here'
    try:
        parse_duration(value)
    except ValueError as exc:
        return str(exc)
    return None


def parse_size(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        match = _SIZE_RE.match(value.strip())
        if match:
            return int(match.group(1)) * _UNIT_BYTES[match.group(2)]
    raise ValueError(f"not a size: {value!r} (use e.g. 64KiB, 1MiB)")


def size_error(value: Any) -> str | None:
    try:
        parse_size(value)
    except ValueError as exc:
        return str(exc)
    return None
