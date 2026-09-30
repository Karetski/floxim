"""Helpers shared by commands: project context, problem output, event printing."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.config import Config, find_project_root, load_config
from floxim.problems import Problem
from floxim.store.events import Event


@dataclass
class GlobalOptions:
    """Flags every command accepts (spec §9.1), set once by `main`."""

    project: Path | None = None
    config: Path | None = None
    quiet: bool = False
    verbose: bool = False
    no_color: bool = False
    as_: str | None = None


OPTIONS = GlobalOptions()


def set_options(args: object) -> None:
    global OPTIONS
    project = getattr(args, "project", None)
    config = getattr(args, "config", None)
    OPTIONS = GlobalOptions(
        project=Path(project) if project else None,
        config=Path(config) if config else None,
        quiet=bool(getattr(args, "quiet", False)),
        verbose=bool(getattr(args, "verbose", False)),
        no_color=bool(getattr(args, "no_color", False)) or bool(os.environ.get("NO_COLOR")),
        as_=getattr(args, "as_", None),
    )


def identity(explicit: str | None = None) -> str | None:
    """Who is acting: a command's own flag, else --as, else $USER."""
    return explicit or OPTIONS.as_ or os.environ.get("USER")


@dataclass
class Context:
    root: Path
    config: Config
    config_problems: list[Problem]


def project_context() -> Context:
    root = OPTIONS.project.resolve() if OPTIONS.project else find_project_root(Path.cwd())
    config, problems = load_config(root, OPTIONS.config)
    return Context(root, config, problems)


def print_problems(problems: Iterable[Problem]) -> None:
    for problem in problems:
        print(problem.render(), file=sys.stderr)


def stderr(message: str) -> None:
    if not OPTIONS.quiet:
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
        if self.quiet or OPTIONS.quiet:
            return
        line = describe(event) if not OPTIONS.verbose else _verbose(event)
        if line:
            stderr(line)


def _verbose(event: Event) -> str:
    from floxim.cli.inspection import format_event

    return format_event(event)


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
