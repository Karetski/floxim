"""Evaluating `next` and `on_error`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from floxim.expr import Clock, EvalError, ExprError, parse
from floxim.templates import parse_template


@dataclass(frozen=True)
class Decision:
    target: str
    case_index: int | None = None
    reason: str | None = None


class RoutingError(Exception):
    """The graph cannot route: `reason` is a run_failed reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def decide(value: Any, namespace: Mapping[str, Any], clock: Clock | None = None) -> Decision:
    """The first case whose `when` is true, or the default. `when` must be a boolean."""
    if isinstance(value, str):
        return Decision(value)
    for index, case in enumerate(value):
        if "when" in case:
            try:
                result = parse(case["when"]).evaluate(namespace, clock)
            except (ExprError, EvalError) as exc:
                raise RoutingError(
                    "expression_error", f"case {index} `when: {case['when']}`: {exc}"
                ) from None
            if not isinstance(result, bool):
                raise RoutingError(
                    "expression_error",
                    f"case {index} `when: {case['when']}` gave {result!r}, not a boolean",
                )
            if not result:
                continue
        reason = None
        if isinstance(case.get("reason"), str):
            try:
                rendered = parse_template(case["reason"]).render(namespace, clock)
            except (ExprError, EvalError) as exc:
                raise RoutingError("expression_error", f"case {index} reason: {exc}") from None
            reason = rendered if isinstance(rendered, str) else str(rendered)
        return Decision(case["to"], index, reason)
    raise RoutingError("no_route", "no case matched and there is no default (E-NO-ROUTE)")
