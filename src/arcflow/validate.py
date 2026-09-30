"""`arcflow validate`: every check of spec §9.4 on one flow file."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arcflow.adapters.registry import PENDING as PENDING_ADAPTERS
from arcflow.adapters.registry import is_aap
from arcflow.checks import (
    expression_checks,
    graph_checks,
    harness_checks,
    limit_checks,
    lint_checks,
    workspace_checks,
)
from arcflow.config import DEFAULT_RISKY_COMMANDS, Config, find_project_root
from arcflow.flow import Flow, load_flow
from arcflow.graph import build_graph
from arcflow.problems import Problem

# Node types the runner can execute in this version. Validation reports
# E-NOT-IMPLEMENTED for the others as its last stage; each milestone that makes a
# node type runnable adds it here (docs/milestones.md).
IMPLEMENTED_NODE_TYPES: frozenset[str] = frozenset(
    {"condition", "sleep", "set", "shell", "python", "agent", "human"}
)


@dataclass
class Report:
    path: Path
    flow: Flow | None
    problems: list[Problem]

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.is_error]

    def ok(self, strict: bool = False) -> bool:
        if strict:
            return not any(p.code[0] in "EW" for p in self.problems)
        return not self.errors


def validate(
    path: Path,
    *,
    config: Config | None = None,
    workdir: Path | None = None,
    implementation_gate: bool = True,
) -> Report:
    """Run the checks in the order of §9.4, stopping after the first stage with errors."""
    flow, problems = load_flow(path)
    if flow is not None:
        graph = build_graph(flow)
        risky = list(config["risky_commands"]) if config else list(DEFAULT_RISKY_COMMANDS)
        root = config.root if config else find_project_root(path.parent)
        stages: list[Callable[[], list[Problem]]] = [
            lambda: graph_checks(flow, graph),
            lambda: expression_checks(flow, graph),
            lambda: limit_checks(flow),
            lambda: harness_checks(flow, config["prices"] if config else None, root),
            lambda: workspace_checks(flow, workdir or path.parent),
            lambda: lint_checks(flow, graph, risky),
        ]
        if implementation_gate:
            stages.append(lambda: _not_implemented(flow, root))
        for stage in stages:
            found = stage()
            problems += found
            if any(p.is_error for p in found):
                break
    problems.sort(key=lambda p: (p.file or "", p.line or 0, p.column or 0, p.code))
    return Report(path, flow, problems)


# Features within implemented node types that the runner cannot execute yet.
PENDING_FEATURES: dict[tuple[str, object], str] = {("on_resume", "ask"): "on_resume: ask"}


def _not_implemented(flow: Flow, root: Path | None = None) -> list[Problem]:
    problems = []
    for node in flow.nodes.values():
        if node.type not in IMPLEMENTED_NODE_TYPES:
            problems.append(
                node.origin.problem(
                    "E-NOT-IMPLEMENTED",
                    f"{node.type} nodes are not available in this version of Arcflow",
                    key=True,
                )
            )
            continue
        harness = node.config.get("harness")
        if node.type == "agent" and harness in PENDING_ADAPTERS:
            problems.append(
                node.where("harness").problem(
                    "E-NOT-IMPLEMENTED",
                    f"the {harness} adapter is not available in this version of Arcflow",
                )
            )
        elif node.type == "agent" and isinstance(harness, str) and is_aap(harness, root):
            problems.append(
                node.where("harness").problem(
                    "E-NOT-IMPLEMENTED",
                    f"{harness} uses the Arcflow Adapter Protocol, which is not available yet",
                )
            )
        for (key, value), what in PENDING_FEATURES.items():
            if node.config.get(key) == value:
                problems.append(
                    node.where(key).problem(
                        "E-NOT-IMPLEMENTED", f"{what} is not available in this version of Arcflow"
                    )
                )
    return problems
