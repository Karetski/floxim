"""Pieces shared by the TUI's screens: status styles, local times, graph, footer, file watching."""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Footer, Static

from floxim.clock import parse_iso
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


def local_time(stamp: str, now: datetime.datetime | None = None) -> str:
    """A stored UTC timestamp, compact, in the local timezone: the time alone for today,
    the date and the minute for an earlier day."""
    moment = parse_iso(stamp).astimezone()
    today = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone().date()
    return moment.strftime("%H:%M:%S" if moment.date() == today else "%b %d %H:%M")


def local_timestamp(stamp: str) -> str:
    """A stored UTC timestamp in full, in the local timezone, named by its abbreviation."""
    return parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


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


class NavKey(Static):
    """A root view's key, pinned in the bottom bar; clicking it presses the key."""

    def __init__(self, key: str, description: str, action: str) -> None:
        label = Content.assemble((f" {key} ", "bold $footer-key-foreground"), f"{description} ")
        super().__init__(label)
        self.action = action

    async def on_click(self) -> None:
        await self.app.run_action(self.action)


class NavFooter(Horizontal):
    """The bottom bar: the root views' keys pinned on the left, so they never scroll away,
    then the screen's own keys in a stock Footer.

    The key of the root view the screen belongs to is highlighted."""

    DEFAULT_CSS = """
    NavFooter { dock: bottom; height: 1; background: $footer-background; }
    NavFooter Footer { dock: none; width: 1fr; }
    NavFooter #nav { width: auto; border-right: vkey $foreground 20%; }
    NavFooter NavKey { width: auto; color: $footer-description-foreground; }
    NavFooter NavKey.-active { text-style: reverse; }
    """
    # The app's actions that show a root view → that view's mode. Their bindings are
    # hidden from the Footer.
    PINNED = {"show_runs": "runs", "show_flows": "flows"}

    def compose(self) -> ComposeResult:
        # The app's own bindings: the screen's live bindings aren't ready while it composes.
        bindings = {b.action: b for b in Binding.make_bindings(self.app.BINDINGS)}
        with Horizontal(id="nav"):
            for action in self.PINNED:
                binding = bindings[action]
                key = self.app.get_key_display(binding)
                yield NavKey(key, binding.description, action)
        yield Footer()

    def on_mount(self) -> None:
        # A screen can compose and mount before the mode switch that created it has updated
        # `current_mode`; the screen-change signal comes after that update.
        self.app.screen_change_signal.subscribe(self, self._highlight)
        self._highlight(self.screen)

    def _highlight(self, screen: Screen[object]) -> None:
        if screen is self.screen:
            for key in self.query(NavKey):
                key.set_class(self.PINNED[key.action] == self.app.current_mode, "-active")


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
