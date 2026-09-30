"""Pieces shared by the TUI's screens: status styles, the graph widget, file watching."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.widgets import Static

from floxim.flow import Flow
from floxim.render import Picture, render

# Status → (marker, style). The marker carries the meaning; colour only helps.
STATUS = {
    "succeeded": ("✓", "green"),
    "failed": ("✗", "red"),
    "running": ("▶", "yellow bold"),
    "waiting": ("…", "magenta bold"),
    "interrupted": ("‖", "yellow"),
    "cancelled": ("⊘", "dim"),
    "timed_out": ("⌛", "red"),
    "pending": ("·", "dim"),
}
RUN_STATUS_ORDER = {"waiting": 0, "running": 1, "interrupted": 2, "pending": 3}


def status_text(status: str) -> Text:
    marker, style = STATUS.get(status, ("?", ""))
    return Text(f"{marker} {status}", style=style)


def node_status(state: dict[str, Any]) -> dict[str, str]:
    """The status of every node that has been visited, for the graph."""
    statuses = {
        node: str(result.get("outcome"))
        for node, result in (state.get("nodes") or {}).items()
        if result
    }
    progress = state.get("in_progress")
    if progress:
        statuses[progress["node"]] = "running"
    for node in state.get("pending_human") or {}:
        statuses[node] = "waiting"
    return statuses


class GraphView(Static):
    """A flow's graph from the shared renderer, coloured by node status."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.picture: Picture | None = None

    def show(
        self,
        flow: Flow,
        statuses: dict[str, str] | None = None,
        visits: dict[str, int] | None = None,
        dim: bool = False,
        highlight: set[str] | None = None,
    ) -> None:
        statuses = statuses or {}
        visits = visits or {}

        def decorate(node: str) -> str:
            mark = STATUS.get(statuses.get(node, ""), ("", ""))[0]
            count = f"×{visits[node]} " if visits.get(node, 0) > 1 else ""
            return f"{mark} {count}" if mark else count

        self.picture = render(flow, decorate)
        text = Text("\n".join(self.picture.lines), style="dim" if dim else "")
        if not dim:
            offsets = _line_offsets(self.picture.lines)
            for node, region in self.picture.regions.items():
                style = STATUS.get(statuses.get(node, ""), ("", ""))[1]
                if node in (highlight or set()):
                    style = f"{style} reverse".strip()
                if not style:
                    continue
                for row in range(region.row, region.row + region.height):
                    start = offsets[row] + region.col
                    text.stylize(style, start, start + region.width)
        self.update(text)


def _line_offsets(lines: list[str]) -> list[int]:
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line) + 1
    return offsets


@dataclass
class FileStamp:
    mtime: float
    size: int
    sha: str


class FileWatcher:
    """Polls a file: cheap mtime and size checks first, a hash when they change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stamp: FileStamp | None = None

    def changed(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        if self.stamp and (stat.st_mtime, stat.st_size) == (self.stamp.mtime, self.stamp.size):
            return False
        sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        changed = self.stamp is None or sha != self.stamp.sha
        self.stamp = FileStamp(stat.st_mtime, stat.st_size, sha)
        return changed
