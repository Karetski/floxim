"""`arcflow validate`: every check of spec §9.4 on one flow file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arcflow.flow import Flow, load_flow
from arcflow.problems import Problem

# Node types the runner can execute in this version. Validation reports
# E-NOT-IMPLEMENTED for the others as its last stage; each milestone that makes a
# node type runnable adds it here (docs/milestones.md).
IMPLEMENTED_NODE_TYPES: frozenset[str] = frozenset()


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


def validate(path: Path, *, implementation_gate: bool = True) -> Report:
    flow, problems = load_flow(path)
    if flow is not None and implementation_gate:
        problems += _not_implemented(flow)
    problems.sort(key=lambda p: (p.file or "", p.line or 0, p.column or 0, p.code))
    return Report(path, flow, problems)


def _not_implemented(flow: Flow) -> list[Problem]:
    return [
        node.origin.problem(
            "E-NOT-IMPLEMENTED",
            f"{node.type} nodes are not available in this version of Arcflow",
            key=True,
        )
        for node in flow.nodes.values()
        if node.type not in IMPLEMENTED_NODE_TYPES
    ]
