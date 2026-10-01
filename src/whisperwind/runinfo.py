"""Reading runs and flows for people and scripts: the library behind `status`,
`list`, `logs`, `artifacts`, `flows` and `graph`. The TUI uses the
same functions."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whisperwind.clock import Clock, parse_iso
from whisperwind.config import Config
from whisperwind.flow import Flow
from whisperwind.graph import build_graph
from whisperwind.store.events import Event, read_log
from whisperwind.store.ids import list_run_ids
from whisperwind.store.lock import lock_state
from whisperwind.store.rundir import RunDir
from whisperwind.store.state import TERMINAL, State
from whisperwind.validate import validate

ACTIVE = frozenset({"pending", "running", "waiting", "interrupted"})
PROGRESS_FIELDS = ("node", "visit", "type", "attempt", "session_id", "wake_at")


def display_status(run: RunDir, state: State, clock: Clock) -> str:
    """The status to show: a `running` run with no live runner is `interrupted`."""
    status = str(state.get("status"))
    if status == "running" and lock_state(run.lock, clock.now()) != "live":
        return "interrupted"
    return status


def summary(run: RunDir, clock: Clock, state: State | None = None) -> dict[str, Any]:
    """One run in a line: what `list` and `status` without a run show."""
    state = state or run.read_state()
    meta = run.meta()
    totals = state.get("totals") or {}
    return {
        "run_id": run.id,
        "flow": meta.get("flow"),
        "status": display_status(run, state, clock),
        "current": state.get("current"),
        "pending": sorted(state.get("pending_human") or {}),
        "created_at": meta.get("created_at"),
        "started_at": (state.get("run") or {}).get("started_at"),
        "finished_at": state.get("finished_at"),
        "parent": meta.get("parent"),
        "totals": {
            "usd_spent": totals.get("usd_spent", 0.0),
            "tokens_spent": totals.get("tokens_spent", 0),
            "steps": totals.get("steps", 0),
            "active_s": round(float(totals.get("active_s", 0.0)), 3),
        },
        "failure": state.get("failure"),
    }


def detail(run: RunDir, clock: Clock) -> dict[str, Any]:
    """Everything `whisperwind status <run>` shows."""
    state = run.read_state()
    meta = run.meta()
    progress = state.get("in_progress")
    nodes = {
        node: {
            "outcome": result.get("outcome"),
            "visit": result.get("visit"),
            "error": result.get("error"),
        }
        for node, result in (state.get("nodes") or {}).items()
        if result
    }
    return {
        **summary(run, clock, state),
        "inputs": meta.get("inputs"),
        "workdir": meta.get("workdir"),
        "visits": state.get("visits"),
        "nodes": nodes,
        "in_progress": None if not progress else {k: progress.get(k) for k in PROGRESS_FIELDS},
        "pending_human": state.get("pending_human") or {},
        "outputs": state.get("outputs"),
        "vars": state.get("vars"),
    }


@dataclass
class RunFilter:
    flow: str | None = None
    statuses: frozenset[str] | None = None
    since: datetime.datetime | None = None
    limit: int | None = None
    active_only: bool = False


def list_runs(runs_dir: Path, clock: Clock, filters: RunFilter) -> list[dict[str, Any]]:
    """Runs newest first, filtered."""
    out: list[dict[str, Any]] = []
    for run_id in reversed(list_run_ids(runs_dir)):
        run = RunDir(runs_dir / run_id)
        try:
            item = summary(run, clock)
        except (OSError, ValueError, KeyError):
            continue
        if filters.flow and item["flow"] != filters.flow:
            continue
        if filters.statuses and item["status"] not in filters.statuses:
            continue
        if filters.active_only and item["status"] not in ACTIVE:
            continue
        if filters.since and item["created_at"] and parse_iso(item["created_at"]) < filters.since:
            continue
        out.append(item)
        if filters.limit and len(out) >= filters.limit:
            break
    return out


def events(run: RunDir, node: str | None = None, visit: int | None = None) -> list[Event]:
    selected = read_log(run.events).events
    if node is not None:
        selected = [
            e
            for e in selected
            if e.get("node") == node or (e.get("data") or {}).get("from") == node
        ]
    if visit is not None:
        selected = [e for e in selected if e.get("visit") == visit]
    return selected


def visit_files(run: RunDir, node: str, visit: int | None, name: str) -> list[Path]:
    """Files named `name` in a node's visit directories (and their attempt directories)."""
    base = run.path / "nodes" / node
    if not base.is_dir():
        return []
    visits = (
        [base / str(visit)]
        if visit is not None
        else sorted((p for p in base.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    )
    found: list[Path] = []
    for directory in visits:
        found += sorted(directory.glob(name)) + sorted(directory.glob(f"attempt-*/{name}"))
    return found


def artifacts(run: RunDir, node: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    nodes_dir = run.path / "nodes"
    if not nodes_dir.is_dir():
        return out
    for node_dir in sorted(nodes_dir.iterdir()):
        if node is not None and node_dir.name != node:
            continue
        visits = sorted(
            (p for p in node_dir.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)
        )
        for visit_dir in visits:
            artifacts_dir = visit_dir / "artifacts"
            if not artifacts_dir.is_dir():
                continue
            for path in sorted(p for p in artifacts_dir.rglob("*") if p.is_file()):
                out.append(
                    {
                        "node": node_dir.name,
                        "visit": int(visit_dir.name),
                        "path": str(path),
                        "size": path.stat().st_size,
                    }
                )
    return out


def discover_flows(config: Config) -> list[dict[str, Any]]:
    """Flow files under `flow_paths`, with name, description and validity."""
    out = []
    for directory in config.flow_paths:
        if not directory.is_dir():
            continue
        for path in sorted([*directory.rglob("*.yaml"), *directory.rglob("*.yml")]):
            if not _looks_like_flow(path):
                continue
            report = validate(path, config=config, workdir=config.root)
            flow = report.flow
            out.append(
                {
                    "file": str(path),
                    "name": flow.name if flow else None,
                    "description": (flow.description or "").strip() if flow else None,
                    "valid": report.ok(),
                    "errors": len(report.errors),
                    "warnings": sum(1 for p in report.problems if p.code.startswith("W-")),
                }
            )
    return out


def _looks_like_flow(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "\nnodes:" in f"\n{text}"


# -- graphs --------------------------------------------------------------------------------


def graph_json(flow: Flow) -> dict[str, Any]:
    graph = build_graph(flow)
    return {
        "name": flow.name,
        "start": flow.start,
        "nodes": [
            {"id": node.id, "type": node.type, "description": node.config.get("description")}
            for node in flow.nodes.values()
        ],
        "edges": [
            {
                "from": e.source,
                "to": e.target,
                "via": e.via,
                "case_index": e.case_index,
                "when": e.when,
                "explicit": e.explicit,
            }
            for e in graph.edges
            if e.explicit or e.via == "next"
        ],
    }


def graph_mermaid(flow: Flow) -> str:
    data = graph_json(flow)
    lines = ["flowchart TD"]
    for node in data["nodes"]:
        lines.append(f'  {node["id"]}["{node["id"]}<br/><small>{node["type"]}</small>"]')
    lines.append("  end_((end))")
    lines.append("  fail_((fail))")
    for edge in data["edges"]:
        target = {"end": "end_", "fail": "fail_"}.get(edge["to"], edge["to"])
        label = edge["when"] or ("on_error" if edge["via"] == "on_error" else "")
        style = "-.->" if edge["via"] == "on_error" else "-->"
        text = f"|{_mermaid_text(label)}|" if label else ""
        lines.append(f"  {edge['from']} {style}{text} {target}")
    return "\n".join(lines) + "\n"


def _mermaid_text(text: str) -> str:
    return text.replace('"', "'").replace("|", "¦")[:60]


def graph_dot(flow: Flow) -> str:
    data = graph_json(flow)
    lines = [f'digraph "{data["name"]}" {{', "  rankdir=TB;", "  node [shape=box];"]
    lines.append('  "end" [shape=doublecircle]; "fail" [shape=doublecircle];')
    for node in data["nodes"]:
        lines.append(f'  "{node["id"]}" [label="{node["id"]}\\n{node["type"]}"];')
    for edge in data["edges"]:
        attrs = []
        if edge["when"]:
            attrs.append('label="' + edge["when"].replace('"', '\\"')[:60] + '"')
        if edge["via"] == "on_error":
            attrs.append("style=dashed")
        suffix = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f'  "{edge["from"]}" -> "{edge["to"]}"{suffix};')
    lines.append("}")
    return "\n".join(lines) + "\n"


def is_terminal(status: str) -> bool:
    return status in TERMINAL
