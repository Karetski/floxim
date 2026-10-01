"""`status`, `list`, `logs`, `wait`, `artifacts`, `flows` and `graph`."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path
from typing import Any

from whisperwind import runinfo
from whisperwind.cli.common import print_problems, project_context, stderr
from whisperwind.clock import Clock
from whisperwind.engine.respond import enforce_timeouts
from whisperwind.exitcodes import ExitCode
from whisperwind.flow import load_flow
from whisperwind.output import emit_json, error
from whisperwind.store.events import Event
from whisperwind.store.ids import AmbiguousRun, RunNotFound, list_run_ids, resolve_run
from whisperwind.store.rundir import RunDir
from whisperwind.units import parse_duration

FOLLOW_POLL_S = 0.3


def add_parsers(commands: Any, common: argparse.ArgumentParser) -> None:
    status = commands.add_parser(
        "status", parents=[common], help="show a run, or the active runs",
        description="One run in detail; without a run, the active runs.",
    )  # fmt: skip
    status.add_argument("run", nargs="?", help="run ID, unique prefix or suffix, @last")
    status.set_defaults(handler=cmd_status)

    listing = commands.add_parser(
        "list", parents=[common], help="list runs", description="Runs, newest first."
    )
    listing.add_argument("--flow", help="only runs of this flow")
    listing.add_argument("--status", help="comma-separated statuses")
    listing.add_argument("--since", help="only runs created within this duration (e.g. 2d)")
    listing.add_argument("--limit", type=int, help="at most this many runs")
    listing.set_defaults(handler=cmd_list)

    logs = commands.add_parser(
        "logs", parents=[common], help="show a run's event log",
        description="The event log; --raw shows the harness stream, --prompt the rendered prompt.",
    )  # fmt: skip
    logs.add_argument("run", help="run ID, unique prefix or suffix, @last")
    logs.add_argument("--node", help="only this node")
    logs.add_argument("--visit", type=int, help="only this visit")
    logs.add_argument("--follow", action="store_true", help="keep printing until the run ends")
    logs.add_argument("--raw", action="store_true", help="print the raw harness stream")
    logs.add_argument("--prompt", action="store_true", help="print the rendered prompt")
    logs.add_argument("--events", action="store_true", help="print events as JSON lines")
    logs.set_defaults(handler=cmd_logs)

    wait = commands.add_parser(
        "wait", parents=[common], help="wait for a run to finish or wait",
        description="Block until the run is terminal or waiting; exit code by status.",
    )  # fmt: skip
    wait.add_argument("run", help="run ID, unique prefix or suffix, @last")
    wait.add_argument("--timeout", help="give up after this duration (exit 8)")
    wait.set_defaults(handler=cmd_wait)

    arts = commands.add_parser(
        "artifacts", parents=[common], help="list a run's artifacts",
        description="Artifact files written by the run's nodes.",
    )  # fmt: skip
    arts.add_argument("run", help="run ID, unique prefix or suffix, @last")
    arts.add_argument("--node", help="only this node")
    arts.set_defaults(handler=cmd_artifacts)

    flows = commands.add_parser(
        "flows", parents=[common], help="list flow files",
        description="Flow files under flow_paths, with name, description and validity.",
    )  # fmt: skip
    flows.set_defaults(handler=cmd_flows)

    graph = commands.add_parser(
        "graph", parents=[common], help="render a flow's graph", description="Render the graph."
    )
    graph.add_argument("flow", help="flow file")
    graph.add_argument("--format", choices=["ascii", "mermaid", "dot", "json"], default="ascii")
    graph.set_defaults(handler=cmd_graph)


# -- helpers ----------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> tuple[RunDir | None, int]:
    context = project_context()
    runs = context.config.runs_dir
    try:
        run = RunDir(runs / resolve_run(runs, args.run))
    except (RunNotFound, AmbiguousRun) as exc:
        return None, _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    # Touching a waiting run applies a passed human deadline (lazy timeouts).
    enforce_timeouts(run, clock=Clock(), project_root=context.root)
    return run, ExitCode.OK


def _fail(args: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    if getattr(args, "json", False):
        emit_json(False, error=error(code, message))
    else:
        stderr(f"whisperwind: {message}")
    return exit_code


def _money(value: float | None) -> str:
    return "—" if not value else f"~${value:.2f}"


def _row(item: dict[str, Any]) -> str:
    totals = item["totals"]
    pending = f"  waiting on {', '.join(item['pending'])}" if item["pending"] else ""
    current = item["current"] or "-"
    return (
        f"{item['run_id']:<44} {item['status']:<12} {current:<16} "
        f"{_money(totals['usd_spent']):>8}{pending}"
    )


# -- commands -----------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    clock = Clock()
    if args.run is None:
        context = project_context()
        for run_id in list_run_ids(context.config.runs_dir):
            enforce_timeouts(
                RunDir(context.config.runs_dir / run_id), clock=clock, project_root=context.root
            )
        active = runinfo.list_runs(
            context.config.runs_dir, clock, runinfo.RunFilter(active_only=True)
        )
        if args.json:
            emit_json(True, data={"runs": active})
        elif not active:
            stderr("no active runs")
        else:
            for item in active:
                print(_row(item))
        return ExitCode.OK
    run, code = _run(args)
    if run is None:
        return code
    info = runinfo.detail(run, clock)
    if args.json:
        emit_json(True, data=info)
        return ExitCode.OK
    print(f"{info['run_id']}  {info['status']}")
    print(f"  flow      {info['flow']}")
    if info["current"]:
        print(f"  current   {info['current']}")
    totals = info["totals"]
    print(
        f"  spent     {_money(totals['usd_spent'])}, {totals['tokens_spent']} tokens, "
        f"{totals['steps']} steps, {totals['active_s']:g}s active"
    )
    for node, result in info["nodes"].items():
        print(f"  {node:<16} {result['outcome']} (visit {result['visit']})")
    for node, prompt in info["pending_human"].items():
        print(f"  ? {node}: {str(prompt.get('message', '')).strip()}")
        choices = prompt.get("choices")
        if choices:
            values = [c["value"] if isinstance(c, dict) else c for c in choices]
            hint = f"whisperwind respond {info['run_id']} {node} --choice <{'|'.join(values)}>"
            print(f"    answer with: {hint}")
    if info["failure"]:
        print(f"  failure   {info['failure'].get('reason')}: {info['failure'].get('message')}")
    if info["outputs"]:
        print(f"  outputs   {json.dumps(info['outputs'])}")
    return ExitCode.OK


def cmd_list(args: argparse.Namespace) -> int:
    context = project_context()
    since = None
    if args.since:
        seconds = parse_duration(args.since) or 0
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    filters = runinfo.RunFilter(
        flow=args.flow,
        statuses=frozenset(args.status.split(",")) if args.status else None,
        since=since,
        limit=args.limit,
    )
    runs = runinfo.list_runs(context.config.runs_dir, Clock(), filters)
    if args.json:
        emit_json(True, data={"runs": runs})
    else:
        for item in runs:
            print(_row(item))
    return ExitCode.OK


def format_event(event: Event) -> str:
    """One event as a line for people."""
    ts = str(event.get("ts", ""))[11:19]
    where = event.get("node") or ""
    if event.get("visit"):
        where += f"#{event['visit']}"
    if event.get("attempt") and event["type"].startswith("attempt"):
        where += f".{event['attempt']}"
    data = event.get("data") or {}
    detail = _event_detail(event["type"], data)
    return f"{ts} {event['seq']:>5} {event['type']:<18} {where:<18} {detail}".rstrip()


def _event_detail(kind: str, data: dict[str, Any]) -> str:
    if kind == "visit_finished":
        error = (data.get("result") or {}).get("error")
        return str(data.get("outcome")) + (f": {error.get('message')}" if error else "")
    if kind == "attempt_finished":
        return str(data.get("outcome"))
    if kind == "route_taken":
        reason = f" ({data['reason']})" if data.get("reason") else ""
        return f"{data.get('from')} → {data.get('to')} via {data.get('via')}{reason}"
    if kind in ("run_failed",):
        return f"{data.get('reason')}: {data.get('message')}"
    if kind == "human_waiting":
        return str(data.get("message", "")).strip().splitlines()[0] if data.get("message") else ""
    if kind == "human_responded":
        return str(
            data.get("choice") or data.get("text") or ("ack" if data.get("acknowledged") else "")
        )
    if kind == "progress":
        return f"{data.get('kind')}: {data.get('summary')}"
    if kind == "budget_updated":
        return f"{_money(data.get('usd_spent'))}, {data.get('tokens_spent')} tokens"
    if kind in ("warning",):
        return f"{data.get('code')}: {data.get('message')}"
    if kind == "session_started":
        return str(data.get("session_id"))
    if kind == "run_succeeded":
        return json.dumps(data.get("outputs")) if data.get("outputs") else ""
    return ""


def cmd_logs(args: argparse.Namespace) -> int:
    run, code = _run(args)
    if run is None:
        return code
    if args.raw or args.prompt:
        if not args.node:
            return _fail(args, "E-USAGE", "--raw and --prompt need --node", ExitCode.USAGE)
        name = "stream.jsonl" if args.raw else "prompt.md"
        files = runinfo.visit_files(run, args.node, args.visit, name)
        if not files:
            return _fail(args, "E-NOT-FOUND", f"no {name} for {args.node}", ExitCode.NOT_FOUND)
        for path in files:
            sys.stdout.write(path.read_text(encoding="utf-8", errors="replace"))
        return ExitCode.OK
    shown = 0
    clock = Clock()
    while True:
        selected = runinfo.events(run, args.node, args.visit)
        for event in selected[shown:]:
            if args.events or args.json:
                sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                print(format_event(event))
        sys.stdout.flush()
        shown = len(selected)
        if not args.follow:
            return ExitCode.OK
        finished = runinfo.is_terminal(runinfo.display_status(run, run.read_state(), clock))
        if finished and len(runinfo.events(run, args.node, args.visit)) == shown:
            return ExitCode.OK
        time.sleep(FOLLOW_POLL_S)


def cmd_wait(args: argparse.Namespace) -> int:
    from whisperwind.engine.runner import exit_status_code

    run, code = _run(args)
    if run is None:
        return code
    context = project_context()
    clock = Clock()
    limit = parse_duration(args.timeout) if args.timeout else None
    deadline = time.monotonic() + limit if limit is not None else None
    while True:
        enforce_timeouts(run, clock=clock, project_root=context.root)
        status = runinfo.display_status(run, run.read_state(), clock)
        if runinfo.is_terminal(status) or status == "waiting":
            break
        if deadline is not None and time.monotonic() >= deadline:
            if args.json:
                emit_json(
                    False, error=error("E-TIMEOUT", "the run is still going", {"status": status})
                )
            else:
                stderr(f"run {run.id} is still {status}")
            return ExitCode.DETACHED
        time.sleep(FOLLOW_POLL_S)
    if args.json:
        emit_json(True, data=runinfo.summary(run, clock))
    else:
        stderr(f"run {run.id} {status}")
    return exit_status_code(status)


def cmd_artifacts(args: argparse.Namespace) -> int:
    run, code = _run(args)
    if run is None:
        return code
    found = runinfo.artifacts(run, args.node)
    if args.json:
        emit_json(True, data={"artifacts": found})
    else:
        for item in found:
            print(f"{item['node']}#{item['visit']}  {item['size']:>8}  {item['path']}")
    return ExitCode.OK


def cmd_flows(args: argparse.Namespace) -> int:
    context = project_context()
    found = runinfo.discover_flows(context.config)
    if args.json:
        emit_json(True, data={"flows": found})
    else:
        for item in found:
            mark = "✓" if item["valid"] else "✗"
            name = item["name"] or "?"
            description = f"  {item['description'].splitlines()[0]}" if item["description"] else ""
            print(f"{mark} {name:<24} {item['file']}{description}")
    return ExitCode.OK


def cmd_graph(args: argparse.Namespace) -> int:
    flow, problems = load_flow(Path(args.flow))
    if flow is None:
        if args.json:
            emit_json(
                False,
                error=error(
                    "E-INVALID-FLOW",
                    "the flow is invalid",
                    {"problems": [p.to_json() for p in problems]},
                ),
            )
        else:
            print_problems(problems)
        return ExitCode.INVALID
    fmt = "json" if args.json else args.format
    if fmt == "json":
        data = runinfo.graph_json(flow)
        if args.json:
            emit_json(True, data=data)
        else:
            print(json.dumps(data, indent=2))
    elif fmt == "mermaid":
        sys.stdout.write(runinfo.graph_mermaid(flow))
    elif fmt == "dot":
        sys.stdout.write(runinfo.graph_dot(flow))
    else:
        from whisperwind.render import render_ascii

        sys.stdout.write(render_ascii(flow))
    return ExitCode.OK
