"""Finding adapters by name (spec §8.1–8.6).

Built-in adapters are always known. Plugin adapters are Python entry points in
the `arcflow.adapters` group, loaded only when a flow names them, so the CLI
starts fast.
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

from arcflow.adapters import Adapter, Capabilities

BUILT_IN = ("claude", "codex", "fake")
# Built-in adapters whose milestone has not landed (docs/milestones.md).
PENDING: frozenset[str] = frozenset({"claude", "codex"})
ENTRY_POINT_GROUP = "arcflow.adapters"


class UnknownAdapter(Exception):
    pass


def _entry_points() -> dict[str, metadata.EntryPoint]:
    return {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}


def names() -> list[str]:
    return sorted({*BUILT_IN, *_entry_points()})


def is_known(name: str) -> bool:
    return name in BUILT_IN or name in _entry_points()


def load(name: str) -> Adapter:
    """A fresh adapter instance. Raises UnknownAdapter."""
    if name == "fake":
        from arcflow.adapters.fake import FakeAdapter

        return FakeAdapter()
    if name in PENDING:
        raise UnknownAdapter(f"the {name} adapter is not available in this version of Arcflow")
    entry = _entry_points().get(name)
    if entry is None:
        raise UnknownAdapter(f"no adapter named {name!r}")
    adapter_class: Any = entry.load()
    adapter: Adapter = adapter_class()
    return adapter


def capabilities(name: str) -> Capabilities | None:
    """Capabilities of an adapter, or None when it cannot be loaded here."""
    try:
        return load(name).capabilities()
    except UnknownAdapter:
        return None
