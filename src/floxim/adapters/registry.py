"""Finding adapters by name.

Names resolve in this order: built-in adapters, then command adapters declared
in the project's `.floxim/harnesses/<name>.yaml`, then Python entry points in
the `floxim.adapters` group. Entry points load only when a flow names them, so
the CLI starts fast.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

from floxim.adapters import Adapter, Capabilities
from floxim.problems import Problem

BUILT_IN = ("claude", "codex", "fake")
# Built-in adapter names that are reserved but not yet runnable.
PENDING: frozenset[str] = frozenset()
ENTRY_POINT_GROUP = "floxim.adapters"


class UnknownAdapter(Exception):
    pass


def _entry_points() -> dict[str, metadata.EntryPoint]:
    return {ep.name: ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}


def _command_file(name: str, root: Path | None) -> Path | None:
    if root is None:
        return None
    from floxim.adapters.command import find

    return find(root, name)


def names(root: Path | None = None) -> list[str]:
    from floxim.adapters.command import list_names

    return sorted({*BUILT_IN, *(list_names(root) if root else []), *_entry_points()})


def is_known(name: str, root: Path | None = None) -> bool:
    return name in BUILT_IN or _command_file(name, root) is not None or name in _entry_points()


def source(name: str, root: Path | None = None) -> str:
    """Where an adapter comes from, for `floxim adapters`."""
    if name in BUILT_IN:
        return "built-in"
    path = _command_file(name, root)
    if path is not None:
        return str(path)
    entry = _entry_points().get(name)
    if entry is not None:
        dist = entry.dist
        return f"{entry.value} ({dist.name} {dist.version})" if dist else entry.value
    return "unknown"


def is_aap(name: str, root: Path | None = None) -> bool:
    """A harness file declaring the Agent Adapter Protocol, which is not supported yet."""
    path = _command_file(name, root)
    if path is None:
        return False
    from floxim.yamlio import load_file

    doc, _ = load_file(path)
    return bool(doc and isinstance(doc.data, dict) and doc.data.get("protocol") == "aap")


def file_problems(name: str, root: Path | None = None) -> list[Problem]:
    """Problems in a command adapter's harness file."""
    path = _command_file(name, root)
    if path is None:
        return []
    from floxim.adapters.command import load_spec

    return load_spec(path)[1]


def load(name: str, root: Path | None = None) -> Adapter:
    """A fresh adapter instance. Raises UnknownAdapter."""
    if name == "fake":
        from floxim.adapters.fake import FakeAdapter

        return FakeAdapter()
    if name == "claude":
        from floxim.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()
    if name == "codex":
        from floxim.adapters.codex import CodexAdapter

        return CodexAdapter()
    path = _command_file(name, root)
    if path is not None:
        if is_aap(name, root):
            raise UnknownAdapter(f"{name}: the Agent Adapter Protocol is not available yet")
        from floxim.adapters.command import CommandAdapter, load_spec

        spec, problems = load_spec(path)
        if spec is None:
            raise UnknownAdapter(f"{path}: {problems[0].message if problems else 'invalid'}")
        command_adapter: Adapter = CommandAdapter(spec)
        return command_adapter
    entry = _entry_points().get(name)
    if entry is None:
        raise UnknownAdapter(f"no adapter named {name!r}")
    adapter_class: Any = entry.load()
    adapter: Adapter = adapter_class()
    return adapter


def capabilities(name: str, root: Path | None = None) -> Capabilities | None:
    """Capabilities of an adapter, or None when it cannot be loaded here."""
    try:
        return load(name, root).capabilities()
    except UnknownAdapter:
        return None
