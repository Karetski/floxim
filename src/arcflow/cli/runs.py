"""`arcflow run` (spec §9.3)."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

from arcflow.cli.common import EventPrinter, print_problems, project_context, stderr
from arcflow.clock import Clock
from arcflow.engine.inputs import InputError, load_inputs_file, parse_assignments
from arcflow.engine.runner import (
    FlowInvalid,
    ResumeOptions,
    ResumeRefused,
    Runner,
    RunOutcome,
    crash_hook_from_env,
    create_run,
    exit_status_code,
)
from arcflow.exitcodes import ExitCode
from arcflow.output import emit_json, error
from arcflow.store.ids import AmbiguousRun, RunNotFound, resolve_run
from arcflow.store.lock import LockHeld
from arcflow.store.rundir import RunDir


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

    resume = commands.add_parser(
        "resume",
        parents=[common],
        help="continue a run",
        description="Continue a run from its last checkpoint (spec §7.5, §7.6).",
    )
    resume.add_argument("run", help="run ID, unique prefix or suffix, @last or @last:<flow>")
    resume.add_argument("--reload", action="store_true", help="use the current flow file")
    resume.add_argument("--from", dest="from_node", metavar="NODE", help="continue at NODE")
    resume.add_argument(
        "--rerun", action="store_true", help="rerun an interrupted visit from scratch"
    )
    resume.add_argument("--force", action="store_true", help="resume a failed or cancelled run")
    resume.add_argument("--events", action="store_true", help="stream events as JSON lines")
    resume.set_defaults(handler=cmd_resume)


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
        crash_hook=crash_hook_from_env(os.environ),
    )
    return _drive(runner, run, args)


def cmd_resume(args: argparse.Namespace) -> int:
    context = project_context()
    try:
        run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
    except (RunNotFound, AmbiguousRun) as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    options = ResumeOptions(
        reload=args.reload, from_node=args.from_node, rerun=args.rerun, force=args.force
    )
    try:
        runner = Runner(
            run,
            context.config,
            Clock(),
            on_event=EventPrinter(as_json=args.events, quiet=args.json),
            crash_hook=crash_hook_from_env(os.environ),
            resume=options,
        )
        runner.check_resumable()
    except ResumeRefused as exc:
        return _fail(args, "E-RESUME-REFUSED", str(exc), ExitCode.USAGE)
    stderr(f"resuming {run.id}")
    try:
        return _drive(runner, run, args)
    except LockHeld as exc:
        return _fail(args, "E-LOCKED", str(exc), ExitCode.CONFLICT)
    except ResumeRefused as exc:
        return _fail(args, "E-RESUME-REFUSED", str(exc), ExitCode.USAGE)


def _fail(args: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    if args.json:
        emit_json(False, error=error(code, message))
    else:
        stderr(f"arcflow: {message}")
    return exit_code


def _drive(runner: Runner, run: RunDir, args: argparse.Namespace) -> int:
    """Run in the foreground. SIGINT or SIGTERM leaves the run resumable; a second
    SIGINT within three seconds skips the grace period (spec §6.7)."""
    last_interrupt = 0.0

    def on_signal(signum: int) -> None:
        nonlocal last_interrupt
        now = time.monotonic()
        urgent = signum == signal.SIGINT and now - last_interrupt < 3.0
        if signum == signal.SIGINT:
            last_interrupt = now
        stderr("stopping; the run stays resumable" + (" (now)" if urgent else ""))
        runner.request_shutdown(urgent=urgent)

    async def main() -> RunOutcome:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, on_signal, signum)
        return await runner.run()

    outcome = asyncio.run(main())
    if outcome.status == "detached":
        stderr(f"run {run.id} is resumable: arcflow resume {run.id}")
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
