"""`arcflow run` (spec §9.3)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from arcflow.cli.common import EventPrinter, print_problems, project_context, stderr
from arcflow.clock import Clock
from arcflow.engine.inputs import InputError, load_inputs_file, parse_assignments
from arcflow.engine.runner import FlowInvalid, Runner, create_run, exit_status_code
from arcflow.exitcodes import ExitCode
from arcflow.output import emit_json, error


def add_parsers(commands: Any, common: argparse.ArgumentParser) -> None:
    run = commands.add_parser(
        "run",
        parents=[common],
        help="create and start a run",
        description="Create and start a run.",
    )
    run.add_argument("flow", help="flow file")
    run.add_argument(
        "--input", action="append", default=[], metavar="NAME=VALUE",
        help="an input value; NAME=@file reads it from a file",
    )  # fmt: skip
    run.add_argument("--inputs-file", type=Path, help="YAML or JSON file of input values")
    run.add_argument("--workdir", type=Path, help="run working directory (default: project root)")
    run.add_argument("--events", action="store_true", help="stream events as JSON lines")
    run.set_defaults(handler=cmd_run)


def cmd_run(args: argparse.Namespace) -> int:
    context = project_context()
    if any(p.is_error for p in context.config_problems):
        print_problems(context.config_problems)
        return ExitCode.USAGE
    clock = Clock()
    workdir = (args.workdir or context.root).resolve()
    try:
        given: dict[str, Any] = {}
        if args.inputs_file is not None:
            given.update(load_inputs_file(args.inputs_file))
        assigned = parse_assignments(args.input, Path.cwd())
        given.update(assigned)
        run = create_run(
            Path(args.flow),
            given,
            config=context.config,
            clock=clock,
            workdir=workdir,
            from_text=set(assigned),
        )
    except FlowInvalid as exc:
        problems = exc.report.problems
        if args.json:
            details = {"problems": [p.to_json() for p in problems]}
            emit_json(False, error=error("E-INVALID-FLOW", "the flow is invalid", details))
        else:
            print_problems(problems)
        return ExitCode.INVALID
    except InputError as exc:
        if args.json:
            emit_json(False, error=error("E-INVALID-INPUT", str(exc)))
        else:
            stderr(f"arcflow: {exc}")
        return ExitCode.INVALID
    stderr(f"run {run.id}")
    runner = Runner(
        run,
        context.config,
        clock,
        on_event=EventPrinter(as_json=args.events, quiet=args.json),
    )
    outcome = asyncio.run(runner.run())
    if args.json:
        state = outcome.state
        data = {
            "run_id": run.id,
            "status": outcome.status,
            "outputs": state.get("outputs"),
            "failure": state.get("failure"),
            "totals": state.get("totals"),
        }
        if outcome.status == "succeeded":
            emit_json(True, data=data)
        else:
            emit_json(False, error=error("E-RUN-" + outcome.status.upper(), outcome.status, data))
    return exit_status_code(outcome.status)
