"""Pieces shared by the TUI's screens: status styles, local times, graph, footer, file watching."""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import geometry
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.events import Click
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Static

from whisperwind.clock import Clock, parse_iso
from whisperwind.flow import Flow
from whisperwind.render import Picture, Region, render

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
# The selected node's box, distinct from the status styles and the "changed" `reverse`.
SELECTED_STYLE = "on #1f3a5f"
RUN_STATUS_ORDER = {"waiting": 0, "running": 1, "interrupted": 2, "pending": 3}


def status_text(status: str) -> Text:
    marker, style = STATUS.get(status, ("?", ""))
    return Text(f"{marker} {status}", style=style)


def local_time(stamp: str, now: datetime.datetime | None = None) -> str:
    """A stored UTC timestamp, compact, in the local timezone: the time alone for today,
    the date and the minute for an earlier day."""
    moment = parse_iso(stamp).astimezone()
    today = (now or Clock().now()).astimezone().date()
    return moment.strftime("%H:%M:%S" if moment.date() == today else "%b %d %H:%M")


def local_timestamp(stamp: str) -> str:
    """A stored UTC timestamp in full, in the local timezone, named by its abbreviation."""
    return parse_iso(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def node_status(state: dict[str, Any], run_status: str) -> dict[str, str]:
    """The status of every node that has been visited, for the graph. The node in
    progress shares the run's status: running, or interrupted when no runner is live."""
    statuses = {
        node: str(result.get("outcome"))
        for node, result in (state.get("nodes") or {}).items()
        if result
    }
    progress = state.get("in_progress")
    if progress:
        statuses[progress["node"]] = run_status
    for node in state.get("pending_human") or {}:
        statuses[node] = "waiting"
    return statuses


def neighbour(regions: dict[str, Region], node: str | None, direction: str) -> str | None:
    """The box an arrow key moves the cursor to from `node`'s: up and down to the nearest
    row of boxes above or below, to the box closest in column; left and right to the
    nearest box in the same row. The top-left box when `node` has none; None when no box
    lies that way."""
    if node not in regions:
        return min(regions, key=lambda n: (regions[n].row, regions[n].col), default=None)
    here = regions[node]

    def centre(box: Region) -> float:
        return box.col + box.width / 2

    if direction in ("left", "right"):
        sign = -1 if direction == "left" else 1
        row = [
            n
            for n, box in regions.items()
            if box.row == here.row and box.col * sign > here.col * sign
        ]
        return min(row, key=lambda n: abs(regions[n].col - here.col), default=None)
    sign = -1 if direction == "up" else 1
    rows = [box.row for box in regions.values() if box.row * sign > here.row * sign]
    if not rows:
        return None
    target = min(rows, key=lambda row: abs(row - here.row))
    return min(
        (n for n, box in regions.items() if box.row == target),
        key=lambda n: abs(centre(regions[n]) - centre(here)),
    )


class GraphView(Static):
    """A flow's graph from the shared renderer, coloured by node status.

    The screen owns the selection: arrow keys in the `GraphPane` and clicks ask it to move
    by posting `Picked`, and `enter` posts `Chosen`; the screen redraws with the node it
    settles on."""

    # A press picks a node, so it must not also start selecting text.
    ALLOW_SELECT = False

    class Picked(Message):
        """A node was picked in the graph, with an arrow key or a click."""

        def __init__(self, node: str) -> None:
            super().__init__()
            self.node = node

    class Chosen(Message):
        """`enter` was pressed on the selected node."""

        def __init__(self, node: str) -> None:
            super().__init__()
            self.node = node

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.picture: Picture | None = None
        self.selected: str | None = None

    def move(self, direction: str) -> None:
        node = neighbour(self.picture.regions, self.selected, direction) if self.picture else None
        if node is not None:
            self.post_message(self.Picked(node))

    def choose(self) -> None:
        if self.selected is not None:
            self.post_message(self.Chosen(self.selected))

    def on_click(self, event: Click) -> None:
        at = event.get_content_offset(self)
        if at is None or self.picture is None:
            return
        for node, box in self.picture.regions.items():
            if box.row <= at.y < box.row + box.height and box.col <= at.x < box.col + box.width:
                self.post_message(self.Picked(node))
                return

    def reveal(self, node: str | None) -> None:
        """Scroll the pane holding the graph, after its next layout, just enough to show
        `node`'s box."""
        box = self.picture.regions.get(node) if self.picture and node else None
        if box is not None and isinstance(self.parent, Widget):
            area = geometry.Region(box.col, box.row, box.width, box.height)
            self.call_after_refresh(self.parent.scroll_to_region, area, animate=False)

    def show(
        self,
        flow: Flow,
        statuses: dict[str, str] | None = None,
        visits: dict[str, int] | None = None,
        dim: bool = False,
        highlight: set[str] | None = None,
        selected: str | None = None,
        reached: set[str] | None = None,
    ) -> None:
        """Draw the flow. `highlight` marks changed nodes, `selected` the node being
        inspected; when `reached` is given, the nodes outside it are dimmed."""
        statuses = statuses or {}
        visits = visits or {}

        def decorate(node: str) -> str:
            mark = STATUS.get(statuses.get(node, ""), ("", ""))[0]
            count = f"×{visits[node]} " if visits.get(node, 0) > 1 else ""
            return f"{mark} {count}" if mark else count

        self.picture = render(flow, decorate)
        self.selected = selected
        text = Text("\n".join(self.picture.lines), style="dim" if dim else "")
        if not dim:
            offsets = _line_offsets(self.picture.lines)
            for node, region in self.picture.regions.items():
                style = STATUS.get(statuses.get(node, ""), ("", ""))[1]
                if node in (highlight or set()):
                    style = f"{style} reverse".strip()
                if reached is not None and node not in reached:
                    style = f"{style} dim".strip()
                if node == selected:
                    style = f"{style} {SELECTED_STYLE}".strip()
                if not style:
                    continue
                for row in range(region.row, region.row + region.height):
                    start = offsets[row] + region.col
                    text.stylize(style, start, start + region.width)
        self.update(text)


class GraphPane(VerticalScroll):
    """The scrolling pane around a `GraphView`, which takes focus and keys for it. The
    graph itself must not take focus: Textual scrolls a focused widget taller than its
    view to the widget's top, away from the selected node."""

    BINDINGS = [
        *(Binding(key, f"move('{key}')", show=False) for key in ("up", "down", "left", "right")),
        Binding("enter", "choose", show=False),
    ]

    def action_move(self, direction: str) -> None:
        self.query_one(GraphView).move(direction)

    def action_choose(self) -> None:
        self.query_one(GraphView).choose()


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
