"""Drawing a flow's graph as text.

A layered (Sugiyama) layout from `grandalf` decides which layer each node sits in
and the order within layers; boxes are drawn with box-drawing characters,
forward edges run downward through per-edge tracks between layers, and back
edges (loops) are routed up a channel on the right in a dashed style. Above
MAX_NODES nodes, or when the layout takes longer than MAX_LAYOUT_S, the graph is
shown as a list in topological order instead. `whisperwind graph --format ascii` and
the TUI use the same renderer, so they never disagree.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from grandalf.graphs import Edge as GEdge
from grandalf.graphs import Graph as GGraph
from grandalf.graphs import Vertex
from grandalf.layouts import SugiyamaLayout

from whisperwind.flow import Flow
from whisperwind.graph import build_graph

MAX_NODES = 60
MAX_LAYOUT_S = 0.2
H_GAP = 3
LABEL_MAX = 18
MIN_LABEL = 6

ICONS = {
    "agent": "◆",
    "shell": "$",
    "condition": "?",
    "human": "☺",
    "sleep": "◷",
    "set": "=",
    "python": "λ",
    "subflow": "⊂",
    "map": "∀",
    "handoff": "↪",
    "notify": "✉",
    "end": "●",
    "fail": "✖",
}

# Direction bits for line characters.
UP, DOWN, LEFT, RIGHT = 1, 2, 4, 8
SOLID = {
    UP | DOWN: "│", LEFT | RIGHT: "─", DOWN | RIGHT: "┌", DOWN | LEFT: "┐",
    UP | RIGHT: "└", UP | LEFT: "┘", UP | DOWN | RIGHT: "├", UP | DOWN | LEFT: "┤",
    DOWN | LEFT | RIGHT: "┬", UP | LEFT | RIGHT: "┴", UP | DOWN | LEFT | RIGHT: "┼",
    UP: "│", DOWN: "│", LEFT: "─", RIGHT: "─",
}  # fmt: skip
DASHED = {**SOLID, UP | DOWN: "┆", LEFT | RIGHT: "┄", UP: "┆", DOWN: "┆", LEFT: "┄", RIGHT: "┄"}

Decorate = Callable[[str], str]  # node ID → a marker shown before it (status, visit count)


@dataclass
class Region:
    row: int
    col: int
    width: int
    height: int


@dataclass
class Picture:
    lines: list[str]
    regions: dict[str, Region] = field(default_factory=dict)  # where each node's box is
    fallback: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class _Canvas:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], str] = {}
        self.dirs: dict[tuple[int, int], int] = defaultdict(int)
        self.dashed: dict[tuple[int, int], bool] = {}

    def line(self, row: int, col: int, bits: int, dashed: bool = False) -> None:
        self.dirs[(row, col)] |= bits
        self.dashed[(row, col)] = self.dashed.get((row, col), True) and dashed

    def put(self, row: int, col: int, text: str) -> None:
        for offset, char in enumerate(text):
            self.cells[(row, col + offset)] = char

    def free_run(self, row: int, col: int, limit: int) -> int:
        """How many cells from `col` on this row are empty, up to `limit`."""
        run = 0
        while run < limit and self.free(row, col + run, 1):
            run += 1
        return run

    def free(self, row: int, col: int, width: int) -> bool:
        return all(
            (row, c) not in self.cells and (row, c) not in self.dirs
            for c in range(col, col + width)
        )

    def lines(self) -> list[str]:
        keys = set(self.cells) | set(self.dirs)
        if not keys:
            return []
        rows = max(r for r, _ in keys) + 1
        cols = max(c for _, c in keys) + 1
        grid = [[" "] * cols for _ in range(rows)]
        for (r, c), bits in self.dirs.items():
            table = DASHED if self.dashed.get((r, c)) else SOLID
            grid[r][c] = table.get(bits, "┼")
        for (r, c), char in self.cells.items():
            grid[r][c] = char
        return ["".join(row).rstrip() for row in grid]

    def vertical(self, col: int, top: int, bottom: int, dashed: bool = False) -> None:
        for row in range(min(top, bottom), max(top, bottom) + 1):
            bits = (UP if row > min(top, bottom) else 0) | (DOWN if row < max(top, bottom) else 0)
            self.line(row, col, bits or (UP | DOWN), dashed)

    def horizontal(self, row: int, left: int, right: int, dashed: bool = False) -> None:
        for col in range(min(left, right), max(left, right) + 1):
            bits = (LEFT if col > min(left, right) else 0) | (
                RIGHT if col < max(left, right) else 0
            )
            if bits:
                self.line(row, col, bits, dashed)


def _label(node_id: str, node_type: str, decorate: Decorate | None) -> str:
    marker = decorate(node_id) if decorate else ""
    icon = ICONS.get(node_type, "•")
    return f"{marker}{icon} {node_id}".strip()


def _short(when: str | None) -> str:
    if not when:
        return ""
    text = " ".join(when.split())
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"


def render(flow: Flow, decorate: Decorate | None = None) -> Picture:
    """The flow's graph as text, or a list when it is too big or slow to lay out."""
    if len(flow.nodes) > MAX_NODES:
        return render_list(flow, decorate)
    started = time.monotonic()
    try:
        picture = _render_layered(flow, decorate, started)
    except _TooSlow:
        return render_list(flow, decorate)
    return picture


class _TooSlow(Exception):
    pass


def _edges(flow: Flow) -> list[tuple[str, str, str | None, bool]]:
    """(source, target, when, on_error) for every edge worth drawing."""
    seen = set()
    edges = []
    for edge in build_graph(flow).edges:
        if not (edge.explicit or edge.via == "next"):
            continue
        # `on_error: continue` routes with next: its edges are next's own.
        key = (edge.source, edge.target, edge.when)
        if key in seen:
            continue
        seen.add(key)
        edges.append((edge.source, edge.target, edge.when, edge.via == "on_error"))
    return edges


def _render_layered(flow: Flow, decorate: Decorate | None, started: float) -> Picture:
    edges = _edges(flow)
    types = {node.id: node.type for node in flow.nodes.values()}
    for _, target, _, _ in edges:
        if target in ("end", "fail"):
            types[target] = target
    labels = {node_id: _label(node_id, types[node_id], decorate) for node_id in types}
    vertices = {node_id: Vertex(node_id) for node_id in types}
    for vertex in vertices.values():
        vertex.view = _View(len(labels[vertex.data]) + 4, 3)
    gedges = [
        GEdge(vertices[s], vertices[t])
        for s, t, _, _ in edges
        if s != t and s in vertices and t in vertices
    ]
    graph = GGraph(list(vertices.values()), gedges)
    layers: list[list[Any]] = []
    for component in graph.C:
        layout = SugiyamaLayout(component)
        roots = [v for v in component.sV if v.data == flow.start] or None
        layout.init_all(roots=roots)
        layout.draw()
        if time.monotonic() - started > MAX_LAYOUT_S:
            raise _TooSlow
        for index, layer in enumerate(layout.layers):
            while len(layers) <= index:
                layers.append([])
            layers[index] += list(layer)
    return _draw(flow, layers, edges, labels)


class _View:
    def __init__(self, w: int, h: int) -> None:
        self.w = w
        self.h = h
        self.xy = (0.0, 0.0)


def _node_of(item: Any) -> str | None:
    """The node ID of a layer item, or None for grandalf's dummy vertices."""
    return item.data if isinstance(item, Vertex) else None


def _draw(
    flow: Flow,
    layers: list[list[Any]],
    edges: list[tuple[str, str, str | None, bool]],
    labels: dict[str, str],
) -> Picture:
    rank: dict[str, int] = {}
    # Place every layer item: real boxes and one-column slots for dummy vertices.
    widths = []
    columns: list[list[tuple[Any, int, int]]] = []  # (item, left, width)
    for index, layer in enumerate(layers):
        x = 0
        placed = []
        for item in layer:
            node = _node_of(item)
            width = len(labels[node]) + 4 if node is not None else 1
            placed.append((item, x, width))
            if node is not None:
                rank[node] = index
            x += width + H_GAP
        columns.append(placed)
        widths.append(max(x - H_GAP, 0))
    total = max(widths or [0])
    for placed, width in zip(columns, widths, strict=True):
        shift = (total - width) // 2
        placed[:] = [(item, left + shift, w) for item, left, w in placed]

    forward = [
        (s, t, w, e) for s, t, w, e in edges if t in rank and s in rank and rank[t] > rank[s]
    ]
    back = [
        (s, t, w, e)
        for s, t, w, e in edges
        if (s, t, w, e) not in forward and s in rank and t in rank
    ]

    # Each forward edge is split along its dummy chain: find, per layer gap, the
    # x at which the edge crosses it.
    item_x: dict[int, int] = {}
    for placed in columns:
        for item, left, width in placed:
            item_x[id(item)] = left + width // 2
    tracks: dict[int, list[tuple[int, int, str, bool]]] = defaultdict(
        list
    )  # gap → (x_from, x_to, label, on_error)
    chains = [(s, t, w, e, _chain(columns, rank, s, t)) for s, t, w, e in forward]
    exits: dict[str, int] = defaultdict(int)
    entries: dict[str, int] = defaultdict(int)
    for source, target, *_ in chains:
        exits[source] += 1
        entries[target] += 1

    def spread(node: str, count: int) -> list[int]:
        item = next(i for placed in columns for i, _, _ in placed if _node_of(i) == node)
        left = next(lft for placed in columns for i, lft, _ in placed if i is item)
        width = len(labels[node]) + 4
        return [left + (k + 1) * width // (count + 1) for k in range(count)]

    exit_x = {node: spread(node, count) for node, count in exits.items()}
    entry_x = {node: spread(node, count) for node, count in entries.items()}
    used_exit: dict[str, int] = defaultdict(int)
    used_entry: dict[str, int] = defaultdict(int)
    segments = []
    for source, target, when, on_error, chain in chains:
        points = [exit_x[source][used_exit[source]]]
        used_exit[source] += 1
        points += [item_x[id(dummy)] for dummy in chain]
        points.append(entry_x[target][used_entry[target]])
        used_entry[target] += 1
        segments.append((rank[source], points, _short(when), on_error))
        for step in range(len(points) - 1):
            tracks[rank[source] + step].append((points[step], points[step + 1], "", on_error))

    # Rows: each layer is a band of box rows, each gap has one track row per edge.
    band_top = []
    row = 0
    gap_rows: dict[int, int] = {}
    for index in range(len(columns)):
        band_top.append(row)
        row += 3
        gap_rows[index] = row
        row += len(tracks.get(index, [])) + 2

    canvas = _Canvas()
    regions: dict[str, Region] = {}
    track_used: dict[int, int] = defaultdict(int)
    # Labels are placed after every line is drawn, at the first candidate spot free.
    pending_labels: list[tuple[list[tuple[int, int]], str]] = []
    for layer_index, points, label, on_error in segments:
        for step in range(len(points) - 1):
            gap = layer_index + step
            top = band_top[gap] + 3  # the row under the boxes
            track = gap_rows[gap] + 1 + track_used[gap]
            track_used[gap] += 1
            bottom = band_top[gap + 1] - 1
            x0, x1 = points[step], points[step + 1]
            canvas.vertical(x0, top, track, dashed=on_error)
            canvas.horizontal(track, x0, x1, dashed=on_error)
            canvas.vertical(x1, track, bottom, dashed=on_error)
            if step == 0 and label:
                spots = [
                    (track, max(x0, x1) + 2),
                    (track, min(x0, x1) - len(label) - 1),
                    (bottom, x1 + 2),
                    (track - 1, max(x0, x1) + 2),
                    (track + 1, x1 + 2),
                ]
                pending_labels.append((spots, label))
            if step < len(points) - 2:
                # Pass straight through the dummy's slot in the next band.
                canvas.vertical(x1, band_top[gap + 1], band_top[gap + 1] + 2, dashed=on_error)
        canvas.put(band_top[layer_index + len(points) - 1] - 1, points[-1], "▼")

    # Back edges and self-loops: up a dashed channel on the right.
    channel = total + 2
    for source, target, when, _on_error in back:
        src_row = band_top[rank[source]] + 1
        dst_row = band_top[rank[target]] + 1
        src_right = _box_right(columns, source, labels)
        dst_right = _box_right(columns, target, labels)
        canvas.horizontal(src_row, src_right + 1, channel, dashed=True)
        canvas.vertical(channel, dst_row, src_row, dashed=True)
        canvas.horizontal(dst_row, dst_right + 1, channel, dashed=True)
        canvas.put(dst_row, dst_right + 1, "◀")
        label = _short(when)
        if label:
            middle = (src_row + dst_row) // 2
            pending_labels.append(([(middle, channel + 2), (middle + 1, channel + 2)], label))
        channel += 3

    for spots, label in pending_labels:
        for row, col in spots:
            if col < 1 or not canvas.free(row, col - 1, 1):
                continue
            room = canvas.free_run(row, col, len(label) + 1) - 1  # keep a space after it
            if room >= min(len(label), MIN_LABEL):
                text = label if room >= len(label) else label[: room - 1] + "…"
                canvas.put(row, col, text)
                break

    # Boxes last, over the lines.
    for index, placed in enumerate(columns):
        for item, left, width in placed:
            node = _node_of(item)
            if node is None:
                continue
            top = band_top[index]
            text = labels[node]
            canvas.put(top, left, "┌" + "─" * (width - 2) + "┐")
            canvas.put(top + 1, left, "│ " + text.ljust(width - 4) + " │")
            canvas.put(top + 2, left, "└" + "─" * (width - 2) + "┘")
            regions[node] = Region(top, left, width, 3)
    return Picture(canvas.lines(), regions)


def _chain(
    columns: list[list[tuple[Any, int, int]]], rank: dict[str, int], source: str, target: str
) -> list[Any]:
    """grandalf's dummy vertices along a long edge, from the source's layer down."""
    dummies = []
    for index in range(rank[source] + 1, rank[target]):
        for item, _, _ in columns[index]:
            if _node_of(item) is None and _dummy_edge(item) == (source, target):
                dummies.append(item)
                break
    return dummies


def _dummy_edge(item: Any) -> tuple[str, str] | None:
    """The (source, target) of the long edge a grandalf dummy vertex stands for:
    its `ctrl` maps ranks to the vertices along the edge, real ones at both ends."""
    ctrl = getattr(item, "ctrl", None)
    if not isinstance(ctrl, dict) or not ctrl:
        return None
    first, last = ctrl[min(ctrl)], ctrl[max(ctrl)]
    if not isinstance(first, Vertex) or not isinstance(last, Vertex):
        return None
    return (first.data, last.data)


def _box_right(columns: list[list[tuple[Any, int, int]]], node: str, labels: dict[str, str]) -> int:
    for placed in columns:
        for item, left, width in placed:
            if _node_of(item) == node:
                return left + width - 1
    raise KeyError(node)


def render_list(flow: Flow, decorate: Decorate | None = None) -> Picture:
    """The fallback: nodes in topological order, each with its edges."""
    order = _topological(flow)
    edges = _edges(flow)
    lines = []
    for node_id in order:
        node = flow.nodes[node_id]
        lines.append(_label(node_id, node.type, decorate))
        for source, target, when, on_error in edges:
            if source != node_id:
                continue
            via = " (on_error)" if on_error else ""
            condition = f"  if {when}" if when else ""
            lines.append(f"    → {target}{via}{condition}")
    return Picture(lines, {}, fallback=True)


def _topological(flow: Flow) -> list[str]:
    graph = build_graph(flow)
    order: list[str] = []
    seen: set[str] = set()

    def visit(node: str) -> None:
        if node in seen or node not in flow.nodes:
            return
        seen.add(node)
        for edge in graph.out(node):
            visit(edge.target)
        order.append(node)

    visit(flow.start)
    for node in flow.nodes:
        visit(node)
    return list(reversed(order))


def render_ascii(flow: Flow) -> str:
    return render(flow).text
