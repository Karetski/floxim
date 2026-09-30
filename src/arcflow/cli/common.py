"""Helpers shared by commands: project context, problem output, event printing."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow.config import Config, find_project_root, load_config
from arcflow.problems import Problem
from arcflow.store.events import Event


@dataclass
class Context:
    root: Path
    config: Config
    config_problems: list[Problem]


def project_context(explicit_config: Path | None = None) -> Context:
    root = find_project_root(Path.cwd())
    config, problems = load_config(root, explicit_config)
    return Context(root, config, problems)


def print_problems(problems: Iterable[Problem]) -> None:
    for problem in problems:
        print(problem.render(), file=sys.stderr)


def stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class EventPrinter:
    """Prints a run's progress for people (stderr) or events as JSON lines (stdout)."""

    def __init__(self, *, as_json: bool, quiet: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet

    def __call__(self, event: Event) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return
        if self.quiet:
            return
        line = describe(event)
        if line:
            stderr(line)


def describe(event: Event) -> str | None:
    kind = event["type"]
    data: dict[str, Any] = event.get("data") or {}
    node = event.get("node")
    if kind == "visit_started":
        visit = event.get("visit")
        return f"▶ {node} ({data.get('type')})" + (f" visit {visit}" if visit and visit > 1 else "")
    if kind == "attempt_started" and (event.get("attempt") or 1) > 1:
        return f"  ↻ {node} attempt {event.get('attempt')}"
    if kind == "visit_finished":
        result = data.get("result") or {}
        outcome = data.get("outcome")
        mark = "✓" if outcome == "succeeded" else "✗"
        detail = f" ({result.get('duration_s', 0):g}s)"
        error = result.get("error")
        if error:
            detail += f": {error.get('message')}"
        return f"{mark} {node} {outcome}{detail}"
    if kind == "route_taken":
        reason = f" ({data['reason']})" if data.get("reason") else ""
        return f"  → {data.get('to')}{reason}"
    if kind == "run_succeeded":
        return "run succeeded"
    if kind == "run_failed":
        return f"run failed: {data.get('message')}"
    if kind == "run_cancelled":
        return "run cancelled"
    if kind == "warning":
        return f"warning {data.get('code')}: {data.get('message')}"
    return None
