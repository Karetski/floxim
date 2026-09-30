"""`arcflow run` (spec §9.3)."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from arcflow.cli.common import EventPrinter, identity, print_problems, project_context, stderr
from arcflow.clock import Clock
from arcflow.engine.human import Answer, InvalidAnswer
from arcflow.engine.inputs import InputError, load_inputs_file, parse_assignments
from arcflow.engine.respond import (
    AlreadyFinished,
    NotWaiting,
    cancel,
    pending_node,
    respond,
    resume_due,
    spawn_detached,
)
from arcflow.engine.runner import (
    FlowInvalid,
    FullPermissionsRefused,
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
    run.add_argument(
        "--allow-full", action="store_true", help="allow agent nodes with permissions: full"
    )
    _wait_options(run)
    run.add_argument(
        "--detach", action="store_true", help="run in the background and return at once"
    )
    run.set_defaults(handler=cmd_run)

    resume = commands.add_parser(
        "resume",
        parents=[common],
        help="continue a run",
        description="Continue a run from its last checkpoint (spec §7.5, §7.6).",
    )
    resume.add_argument(
        "run", nargs="?", help="run ID, unique prefix or suffix, @last or @last:<flow>"
    )
    resume.add_argument(
        "--due",
        action="store_true",
        help="continue every run whose human timeout or sleep has passed (for cron)",
    )
    resume.add_argument("--reload", action="store_true", help="use the current flow file")
    resume.add_argument("--from", dest="from_node", metavar="NODE", help="continue at NODE")
    resume.add_argument(
        "--rerun", action="store_true", help="rerun an interrupted visit from scratch"
    )
    resume.add_argument("--force", action="store_true", help="resume a failed or cancelled run")
    resume.add_argument(
        "--recreate-workspaces", action="store_true", help="recreate deleted worktrees"
    )
    resume.add_argument("--events", action="store_true", help="stream events as JSON lines")
    _wait_options(resume)
    resume.add_argument(
        "--detach", action="store_true", help="continue in the background and return at once"
    )
    resume.set_defaults(handler=cmd_resume)

    respond = commands.add_parser(
        "respond",
        parents=[common],
        help="answer a waiting run",
        description="Answer a pending human node (spec §5.4, §6.11).",
    )
    respond.add_argument("run", help="run ID, unique prefix or suffix, @last or @last:<flow>")
    respond.add_argument("node", nargs="?", help="the human node (needed when several wait)")
    answer = respond.add_mutually_exclusive_group(required=True)
    answer.add_argument("--choice", help="one of the prompt's choices")
    answer.add_argument("--text", help="a free-text answer")
    answer.add_argument("--ack", action="store_true", help="acknowledge")
    respond.add_argument("--comment", help="free text with a choice, when the prompt allows it")
    respond.add_argument("--responder", help="who is answering (default: --as, else $USER)")
    respond.add_argument(
        "--no-continue", action="store_true", help="only record the answer; do not continue the run"
    )
    respond.set_defaults(handler=cmd_respond)

    cancel_parser = commands.add_parser(
        "cancel", parents=[common], help="cancel a run", description="Cancel a run (spec §6.7)."
    )
    cancel_parser.add_argument("run", help="run ID, unique prefix or suffix, @last or @last:<flow>")
    cancel_parser.add_argument("--reason", help="why, recorded with the cancellation")
    cancel_parser.add_argument("--by", help="who is cancelling (default: --as, else $USER)")
    cancel_parser.set_defaults(handler=cmd_cancel)

    handoff = commands.add_parser(
        "handoff",
        parents=[common],
        help="open a pending handoff session",
        description="Open a pending handoff session here, then continue the run (spec §5.10).",
    )
    handoff.add_argument("run", help="run ID, unique prefix or suffix, @last or @last:<flow>")
    handoff.add_argument("node", nargs="?", help="the handoff node (needed when several wait)")
    handoff.add_argument(
        "--no-continue", action="store_true", help="only open the session; do not continue the run"
    )
    handoff.set_defaults(handler=cmd_handoff, events=False, on_wait=None, detach=False)


def cmd_handoff(args: argparse.Namespace) -> int:
    context = project_context()
    try:
        run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
        node, prompt = pending_node(run, args.node)
    except (RunNotFound, AmbiguousRun, NotWaiting) as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    if prompt.get("kind") != "handoff":
        return _fail(args, "E-NOT-FOUND", f"{node!r} is not a handoff", ExitCode.NOT_FOUND)
    command = [str(part) for part in prompt.get("command") or []]
    stderr(f"→ {node}: {str(prompt.get('message') or '').strip()}")
    stderr(f"  opening: {' '.join(command)}")
    code = subprocess.run(command, cwd=context.root, check=False).returncode
    answer = Answer(acknowledged=True, responder=identity(), via="handoff", exit_code=code)
    respond(run, node, answer, clock=Clock(), project_root=context.root, continue_run=False)
    if args.no_continue:
        return ExitCode.OK
    runner = Runner(
        run,
        context.config,
        Clock(),
        on_event=EventPrinter(as_json=False, quiet=args.json),
        on_wait=_on_wait(args),
    )
    return _drive(runner, run, args)


def cmd_cancel(args: argparse.Namespace) -> int:
    context = project_context()
    try:
        run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
    except (RunNotFound, AmbiguousRun) as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    clock = Clock()
    try:
        how = cancel(run, by=identity(args.by), reason=args.reason, clock=clock)
    except AlreadyFinished as exc:
        return _fail(args, "E-ALREADY-FINISHED", str(exc), ExitCode.USAGE)
    except LockHeld as exc:
        return _fail(args, "E-LOCKED", str(exc), ExitCode.CONFLICT)
    status = run.read_state()["status"]
    if how == "requested":
        # The live runner stops the attempt within its grace period and records it.
        deadline = time.monotonic() + 30
        while status != "cancelled" and time.monotonic() < deadline:
            time.sleep(0.2)
            status = run.read_state()["status"]
    if args.json:
        emit_json(True, data={"run_id": run.id, "status": status, "delivered": how})
    else:
        stderr(f"run {run.id} {status}")
    return ExitCode.OK


def _wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--on-wait",
        choices=["prompt", "wait", "exit"],
        help="at a human node: ask here, keep waiting, or exit with code 4 "
        "(default: prompt on a terminal, else exit)",
    )


def _on_wait(args: argparse.Namespace) -> str:
    if args.on_wait:
        return str(args.on_wait)
    if getattr(args, "detach", False) or not sys.stdin.isatty():
        return "exit"
    return "prompt"


def cmd_respond(args: argparse.Namespace) -> int:
    context = project_context()
    try:
        run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
    except (RunNotFound, AmbiguousRun) as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    answer = Answer(
        choice=args.choice,
        text=args.text if args.text is not None else args.comment,
        acknowledged=args.ack,
        responder=identity(args.responder),
        via="cli",
    )
    try:
        delivery = respond(
            run,
            args.node,
            answer,
            clock=Clock(),
            project_root=context.root,
            continue_run=not args.no_continue,
        )
    except NotWaiting as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    except InvalidAnswer as exc:
        return _fail(args, "E-INVALID-ANSWER", str(exc), ExitCode.USAGE)
    except LockHeld as exc:
        return _fail(args, "E-LOCKED", str(exc), ExitCode.CONFLICT)
    data = {
        "run_id": run.id,
        "node": delivery.node,
        "delivered": "inbox" if delivery.via_inbox else "recorded",
        "continued": delivery.continued,
    }
    if args.json:
        emit_json(True, data=data)
    else:
        stderr(
            f"answered {delivery.node} in {run.id}"
            + (" (continuing)" if delivery.continued else "")
        )
    return ExitCode.OK


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
            allow_full=args.allow_full,
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
        return _fail(args, "E-INVALID-INPUT", str(exc), ExitCode.INVALID)
    except FullPermissionsRefused as exc:
        return _fail(args, "E-FULL-PERMISSIONS", str(exc), ExitCode.INVALID)
    stderr(f"run {run.id}")
    if _untracked(Path(args.flow)):
        stderr(
            f"note: {args.flow} is not tracked by git; a flow runs commands as you, "
            "so review it before running it"
        )
    if args.detach:
        return _detach(run, context.root, args)
    runner = Runner(
        run,
        context.config,
        clock,
        on_event=EventPrinter(as_json=args.events, quiet=args.json),
        crash_hook=crash_hook_from_env(os.environ),
        on_wait=_on_wait(args),
    )
    return _drive(runner, run, args)


def _untracked(flow: Path) -> bool:
    """True when the flow file is in a git work tree but not tracked (§12.1)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", flow.name],
            cwd=flow.resolve().parent,
            capture_output=True,
        )
    except OSError:
        return False
    return result.returncode == 1  # 128: not a repository


def _detach(
    run: RunDir, root: Path, args: argparse.Namespace, extra_args: list[str] | None = None
) -> int:
    pid = spawn_detached(run, root, extra_args)
    if args.json:
        emit_json(True, data={"run_id": run.id, "status": "started", "pid": pid})
    else:
        stderr(
            f"running in the background (pid {pid}); follow it with: arcflow logs {run.id} --follow"
        )
    return ExitCode.OK


def cmd_resume(args: argparse.Namespace) -> int:
    context = project_context()
    if args.due:
        if args.run:
            return _fail(args, "E-USAGE", "--due takes no run", ExitCode.USAGE)
        started = resume_due(context.config.runs_dir, clock=Clock(), project_root=context.root)
        if args.json:
            emit_json(True, data={"resumed": started})
        else:
            for run_id in started:
                stderr(f"resumed {run_id}")
        return ExitCode.OK
    if not args.run:
        return _fail(args, "E-USAGE", "name a run, or use --due", ExitCode.USAGE)
    try:
        run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
    except (RunNotFound, AmbiguousRun) as exc:
        return _fail(args, "E-NOT-FOUND", str(exc), ExitCode.NOT_FOUND)
    options = ResumeOptions(
        reload=args.reload,
        from_node=args.from_node,
        rerun=args.rerun,
        force=args.force,
        recreate_workspaces=args.recreate_workspaces,
    )
    try:
        runner = Runner(
            run,
            context.config,
            Clock(),
            on_event=EventPrinter(as_json=args.events, quiet=args.json),
            crash_hook=crash_hook_from_env(os.environ),
            resume=options,
            on_wait=_on_wait(args),
        )
        runner.check_resumable()
    except ResumeRefused as exc:
        return _fail(args, "E-RESUME-REFUSED", str(exc), ExitCode.USAGE)
    stderr(f"resuming {run.id}")
    if args.detach:
        extra = [
            *(["--reload"] if args.reload else []),
            *(["--from", args.from_node] if args.from_node else []),
            *(["--rerun"] if args.rerun else []),
            *(["--force"] if args.force else []),
            *(["--recreate-workspaces"] if args.recreate_workspaces else []),
        ]
        return _detach(run, context.root, args, extra)
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
