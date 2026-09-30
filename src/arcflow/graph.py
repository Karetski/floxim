"""The routing graph of a flow (spec §3.9, §6.5).

Edges exist only in `next` and `on_error`. A node without `next` routes to
`end`; `on_error` defaults to `fail`, and `on_error: continue` routes with
`next`. The implicit `on_error: fail` of every node is an edge like any other
for routing, but the static checks that ask "can this node finish?" look only
at edges the author wrote (`explicit`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from arcflow.flow import Flow, Origin
from arcflow.flowspec import TERMINAL_TARGETS
from arcflow.problems import join_pointer

END = "end"
FAIL = "fail"


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    via: str  # "next" or "on_error"
    case_index: int | None  # None for a single target
    when: str | None
    explicit: bool
    origin: Origin  # where the target is written (the node itself when implicit)

    @property
    def is_default(self) -> bool:
        return self.when is None


@dataclass
class Graph:
    start: str
    nodes: list[str]
    edges: list[Edge] = field(default_factory=list)

    def out(self, node: str, *, explicit_only: bool = False) -> list[Edge]:
        return [e for e in self.edges if e.source == node and (e.explicit or not explicit_only)]

    def successors(self, node: str, *, explicit_only: bool = False) -> set[str]:
        return {e.target for e in self.out(node, explicit_only=explicit_only)}

    def reachable_from(self, node: str, *, explicit_only: bool = False) -> set[str]:
        """Every node or terminal reachable from `node` by one or more edges."""
        seen: set[str] = set()
        stack = list(self.successors(node, explicit_only=explicit_only))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current not in TERMINAL_TARGETS:
                stack.extend(self.successors(current, explicit_only=explicit_only))
        return seen

    def reachable(self) -> set[str]:
        return {self.start} | self.reachable_from(self.start)

    def has_path(self, source: str, target: str) -> bool:
        return target in self.reachable_from(source)

    def cycles(self) -> list[set[str]]:
        """Strongly connected components that contain a cycle (Tarjan's algorithm)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[set[str]] = []
        counter = 0

        def visit(node: str) -> None:
            nonlocal counter
            index[node] = low[node] = counter
            counter += 1
            stack.append(node)
            on_stack.add(node)
            for succ in self.successors(node):
                if succ in TERMINAL_TARGETS:
                    continue
                if succ not in index:
                    visit(succ)
                    low[node] = min(low[node], low[succ])
                elif succ in on_stack:
                    low[node] = min(low[node], index[succ])
            if low[node] == index[node]:
                component: set[str] = set()
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    component.add(top)
                    if top == node:
                        break
                if len(component) > 1 or node in self.successors(node):
                    components.append(component)

        for node in self.nodes:
            if node not in index:
                visit(node)
        return components


def build_graph(flow: Flow) -> Graph:
    graph = Graph(start=flow.start, nodes=list(flow.nodes))
    for node in flow.nodes.values():
        next_value = node.config.get("next", END)
        next_origin = node.where("next")
        next_edges = list(_edges(node.id, "next", next_value, next_origin, "next" in node.config))
        graph.edges += next_edges
        on_error = node.config.get("on_error", FAIL)
        error_origin = node.where("on_error")
        explicit = "on_error" in node.config
        if on_error == "continue":
            graph.edges += [
                Edge(e.source, e.target, "on_error", e.case_index, e.when, explicit, e.origin)
                for e in next_edges
            ]
        else:
            graph.edges += _edges(node.id, "on_error", on_error, error_origin, explicit)
    return graph


def _edges(source: str, via: str, value: Any, origin: Origin, explicit: bool) -> Iterator[Edge]:
    if isinstance(value, str):
        yield Edge(source, value, via, None, None, explicit, origin)
        return
    for index, case in enumerate(value):
        case_origin = Origin(origin.doc, join_pointer(join_pointer(origin.pointer, index), "to"))
        yield Edge(source, case["to"], via, index, case.get("when"), explicit, case_origin)


def cases(value: Any) -> Iterable[tuple[int, dict[str, Any]]]:
    return enumerate(value) if isinstance(value, list) else ()
