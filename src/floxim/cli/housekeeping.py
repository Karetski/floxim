"""`init`, `adapters`, `gc` and `doctor` (spec §9.3, §7.7)."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from floxim import purity
from floxim.adapters import registry
from floxim.cli.common import project_context, stderr
from floxim.clock import Clock, parse_iso
from floxim.exitcodes import ExitCode
from floxim.output import emit_json, error
from floxim.store.events import CorruptLog, EventWriter, read_log
from floxim.store.ids import AmbiguousRun, RunNotFound, list_run_ids, resolve_run
from floxim.store.lock import lock_state, read_lock
from floxim.store.rundir import RunDir
from floxim.store.state import TERMINAL, reduce
from floxim.units import parse_duration

CONFIG_TEMPLATE = """# Floxim project configuration (spec §2.2). It describes the environment flows
# run in; what a flow does lives in the flow file.
flow_paths: [flows]
# env_passthrough: [NPM_TOKEN]      # extra variables allowed into nodes
# harnesses:
#   claude: {tested_versions: ">=2.1.285,<2.2"}
# prices:                           # USD estimates for harnesses that report tokens only
#   codex:
#     gpt-5-codex: {input_per_mtok: 1.25, cached_input_per_mtok: 0.125, output_per_mtok: 10.0}
# on_wait: 'notify-send "Floxim" "$FLOXIM_MESSAGE"'
# redact: ['sk-[A-Za-z0-9_-]{20,}']
"""
GITIGNORE = "runs/\nworktrees/\n"
EXAMPLE_FLOW = """floxim: 1
name: hello
description: A first flow. Run it with `floxim run flows/hello.yaml --input name=you`.

inputs:
  name: {type: string, default: world}

nodes:
  greet:
    type: shell
    run: echo "Hello, $NAME"
    env: {NAME: "${{ inputs.name }}"}
    next: ask
  ask:
    type: human
    message: "Greeted ${{ inputs.name }}. Finish?"
    choices: [finish, again]
    next:
      - when: nodes.ask.choice == "again" and visits.greet < 3
        to: greet
      - to: end

outputs:
  said: ${{ strip(nodes.greet.stdout) }}
"""
NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smbfs", "smb", "afpfs", "webdav", "fuse.sshfs", "9p"}


def add_parsers(commands: Any, common: argparse.ArgumentParser) -> None:
    init = commands.add_parser(
        "init", parents=[common], help="set up .floxim/ in this project",
        description="Create .floxim/ with config and .gitignore, and an example flow.",
    )  # fmt: skip
    init.set_defaults(handler=cmd_init)

    adapters = commands.add_parser(
        "adapters", parents=[common], help="list harness adapters",
        description="Installed adapters, their source and capabilities.",
    )  # fmt: skip
    adapters.add_argument("--probe", action="store_true", help="also check binaries and versions")
    adapters.set_defaults(handler=cmd_adapters)

    gc = commands.add_parser(
        "gc", parents=[common], help="delete old finished runs",
        description="Delete finished run directories and their worktrees (spec §7.7).",
    )  # fmt: skip
    gc.add_argument("--older-than", help="only runs finished this long ago (default: retention)")
    gc.add_argument(
        "--status", help="comma-separated statuses (default: succeeded,failed,cancelled)"
    )
    gc.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    gc.set_defaults(handler=cmd_gc)

    doctor = commands.add_parser(
        "doctor", parents=[common], help="check the environment, or repair a run",
        description="Check Python, git, harnesses, dependencies and the filesystem; "
        "with a run, check and repair its event log and state.",
    )  # fmt: skip
    doctor.add_argument("run", nargs="?", help="a run to check and repair")
    doctor.add_argument(
        "--truncate",
        action="store_true",
        help="cut a corrupt event log before its first bad line (loses what follows)",
    )
    doctor.set_defaults(handler=cmd_doctor)

    tui = commands.add_parser(
        "tui", parents=[common], help="open the terminal UI",
        description="Open the terminal UI, at a flow's graph or a run's detail when given.",
    )  # fmt: skip
    tui.add_argument("target", nargs="?", help="a flow file, or a run (ID, prefix, @last)")
    tui.set_defaults(handler=cmd_tui)


def cmd_tui(args: argparse.Namespace) -> int:
    from floxim.tui.app import FloximApp

    context = project_context()
    target = args.target
    if target and not Path(target).is_file():
        try:
            target = resolve_run(context.config.runs_dir, target)
        except (RunNotFound, AmbiguousRun) as exc:
            stderr(f"floxim: {exc}")
            return ExitCode.NOT_FOUND
    FloximApp(context.config, target).run()
    return ExitCode.OK


# -- init ---------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    context = project_context()
    root = context.root
    files = {
        root / ".floxim" / "config.yaml": CONFIG_TEMPLATE,
        root / ".floxim" / ".gitignore": GITIGNORE,
        root / "flows" / "hello.yaml": EXAMPLE_FLOW,
    }
    created, kept = [], []
    for path, text in files.items():
        if path.exists():
            kept.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        created.append(str(path))
    if args.json:
        emit_json(True, data={"root": str(root), "created": created, "kept": kept})
    else:
        for made in created:
            stderr(f"created {made}")
        for existing in kept:
            stderr(f"kept    {existing} (already there)")
    return ExitCode.OK


# -- adapters -----------------------------------------------------------------------------


def _auth_hint(name: str) -> str | None:
    if name == "claude":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY is set"
        return "uses Claude Code's own login (bare mode needs ANTHROPIC_API_KEY)"
    if name == "codex":
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
            return "an API key is set"
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return "uses the Codex login" if (home / "auth.json").exists() else "not logged in"
    return None


def cmd_adapters(args: argparse.Namespace) -> int:
    context = project_context()
    found = []
    for name in registry.names(context.root):
        item: dict[str, Any] = {"name": name, "source": registry.source(name, context.root)}
        try:
            adapter = registry.load(name, context.root)
        except registry.UnknownAdapter as exc:
            item.update({"loadable": False, "problem": str(exc)})
            found.append(item)
            continue
        configure = getattr(adapter, "configure", None)
        if configure is not None:
            configure(context.config["harnesses"].get(name) or {})
        item.update({"loadable": True, "capabilities": adapter.capabilities().to_json()})
        if args.probe:
            probe = asyncio.run(adapter.probe())
            item["probe"] = {
                "found": probe.found,
                "version": probe.version,
                "in_tested_range": probe.in_tested_range,
                "auth": _auth_hint(name),
            }
        found.append(item)
    if args.json:
        emit_json(True, data={"adapters": found})
        return ExitCode.OK
    for item in found:
        line = f"{item['name']:<10} {item['source']}"
        probed = item.get("probe")
        if probed:
            line += "  " + (f"v{probed['version']}" if probed["found"] else "not found")
            if probed["in_tested_range"] is False:
                line += " (outside tested_versions)"
            if probed["auth"]:
                line += f"; {probed['auth']}"
        if not item.get("loadable", True):
            line += f"  ✗ {item['problem']}"
        print(line)
    return ExitCode.OK


# -- gc -------------------------------------------------------------------------------------


def _merged(branch: str, workdir: Path) -> bool:
    result = subprocess.run(
        ["git", "branch", "--merged", "HEAD", "--list", branch],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def cmd_gc(args: argparse.Namespace) -> int:
    context = project_context()
    clock = Clock()
    days = context.config["retention"].get("keep_days", 30)
    older = parse_duration(args.older_than) if args.older_than else float(days) * 86400
    statuses = set(args.status.split(",")) if args.status else set(TERMINAL)
    cutoff = clock.now() - datetime.timedelta(seconds=older or 0)
    removed: list[str] = []
    worktrees: list[str] = []
    for run_id in list_run_ids(context.config.runs_dir):
        run = RunDir(context.config.runs_dir / run_id)
        state = run.read_state()
        finished = state.get("finished_at")
        if state.get("status") not in statuses or state.get("status") not in TERMINAL:
            continue  # waiting and running runs are never collected
        if lock_state(run.lock, clock.now()) == "live" or not finished:
            continue
        if parse_iso(str(finished)) > cutoff:
            continue
        workdir = Path(run.meta().get("workdir") or context.root)
        for recorded in (state.get("workspaces") or {}).values():
            path = Path(recorded["path"])
            if not path.is_dir():
                continue
            if not recorded.get("keep", True) or _merged(str(recorded["branch"]), workdir):
                worktrees.append(str(path))
                if not args.dry_run:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=workdir,
                        capture_output=True,
                        check=False,
                    )
        removed.append(run_id)
        if not args.dry_run:
            shutil.rmtree(run.path)
    data = {"removed": removed, "worktrees": worktrees, "dry_run": bool(args.dry_run)}
    if args.json:
        emit_json(True, data=data)
    else:
        verb = "would delete" if args.dry_run else "deleted"
        for run_id in removed:
            print(f"{verb} {run_id}")
        for tree in worktrees:
            print(f"{verb} worktree {tree}")
    return ExitCode.OK


# -- doctor ---------------------------------------------------------------------------------


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def _filesystem_type(path: Path) -> str | None:
    """The filesystem type of `path`, best effort (Linux /proc/mounts, macOS mount)."""
    target = str(path.resolve())
    best: tuple[int, str] | None = None
    try:
        if Path("/proc/mounts").exists():
            lines = Path("/proc/mounts").read_text().splitlines()
            entries = [
                (parts[1], parts[2]) for parts in (line.split() for line in lines) if len(parts) > 2
            ]
        else:
            output = subprocess.run(["mount"], capture_output=True, text=True, check=False).stdout
            entries = []
            for line in output.splitlines():
                if " on " in line and "(" in line:
                    mount_point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
                    fstype = line.rsplit("(", 1)[1].split(",")[0].strip(")")
                    entries.append((mount_point, fstype))
    except OSError:
        return None
    for mount_point, fstype in entries:
        inside = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if inside and (best is None or len(mount_point) > best[0]):
            best = (len(mount_point), fstype)
    return best[1] if best else None


def environment_checks(context: Any) -> list[dict[str, str]]:
    checks = []
    version = platform.python_version()
    ok = sys.version_info >= (3, 10)
    checks.append(_check("python", "ok" if ok else "error", f"{version} ({sys.executable})"))
    git = shutil.which("git")
    checks.append(_check("git", "ok" if git else "warn", git or "not found (worktrees need git)"))
    for name in ("claude", "codex"):
        adapter = registry.load(name)
        configure = getattr(adapter, "configure", None)
        if configure is not None:
            configure(context.config["harnesses"].get(name) or {})
        probe = asyncio.run(adapter.probe())
        if not probe.found:
            checks.append(_check(name, "warn", "not found; flows using it cannot run"))
        elif probe.in_tested_range is False:
            checks.append(_check(name, "warn", f"{probe.version} is outside tested_versions"))
        else:
            checks.append(_check(name, "ok", f"{probe.version}"))
    impure = purity.find_impure(purity.installed_wheels(purity.dependency_names()))
    if impure:
        names = ", ".join(name for name, _ in impure)
        checks.append(_check("dependencies", "warn", f"not pure Python: {names}"))
    else:
        checks.append(_check("dependencies", "ok", "all runtime dependencies are pure Python"))
    runs_dir = context.config.runs_dir
    probe_dir = runs_dir if runs_dir.exists() else context.root
    fstype = _filesystem_type(probe_dir)
    if fstype in NETWORK_FILESYSTEMS:
        checks.append(
            _check("filesystem", "warn", f"{probe_dir} is on {fstype}; run locks need a local disk")
        )
    elif not os.access(probe_dir, os.W_OK):
        checks.append(_check("filesystem", "error", f"{probe_dir} is not writable"))
    else:
        checks.append(_check("filesystem", "ok", f"{probe_dir} ({fstype or 'unknown type'})"))
    return checks


def run_checks(run: RunDir, truncate: bool) -> list[dict[str, str]]:
    """Check a run's log and state, repairing what is safe to repair (§7.3)."""
    checks = []
    try:
        log = read_log(run.events)
    except CorruptLog as exc:
        if not truncate:
            return [
                _check("events", "error", f"{exc}; --truncate cuts the log before line {exc.line}")
            ]
        lines = run.events.read_bytes().split(b"\n")
        backup = run.events.with_name("events.jsonl.corrupt")
        shutil.copy2(run.events, backup)
        run.events.write_bytes(b"\n".join(lines[: exc.line - 1]) + b"\n")
        checks.append(
            _check("events", "warn", f"cut at line {exc.line}; the old log is {backup.name}")
        )
        log = read_log(run.events)
    if log.torn_tail or log.needs_newline:
        EventWriter(run.events, Clock()).close()  # trims a torn tail, finishes the last line
        checks.append(_check("events", "warn", "trimmed a torn last line"))
    elif not checks:
        checks.append(_check("events", "ok", f"{len(log.events)} events"))
    state = reduce(read_log(run.events).events)
    try:
        cached = json.loads(run.state_json.read_text())
        cached_ok = isinstance(cached, dict) and cached.get("last_seq") == state["last_seq"]
    except (OSError, ValueError):
        cached_ok = False
    run.write_state(state)
    checks.append(
        _check(
            "state",
            "ok" if cached_ok else "warn",
            "current" if cached_ok else "rebuilt from events",
        )
    )
    lock = read_lock(run.lock)
    if lock is not None and lock_state(run.lock, Clock().now()) == "stale":
        run.lock.unlink(missing_ok=True)
        checks.append(_check("lock", "warn", f"removed a stale lock (pid {lock.get('pid')})"))
    for stray in run.inbox.glob(".*.tmp") if run.inbox.is_dir() else []:
        stray.unlink(missing_ok=True)
        checks.append(_check("inbox", "warn", f"removed an unfinished request {stray.name}"))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    context = project_context()
    if args.run:
        try:
            run = RunDir(context.config.runs_dir / resolve_run(context.config.runs_dir, args.run))
        except (RunNotFound, AmbiguousRun) as exc:
            if args.json:
                emit_json(False, error=error("E-NOT-FOUND", str(exc)))
            else:
                stderr(f"floxim: {exc}")
            return ExitCode.NOT_FOUND
        checks = run_checks(run, args.truncate)
    else:
        checks = environment_checks(context)
    ok = not any(c["status"] == "error" for c in checks)
    if args.json:
        if ok:
            emit_json(True, data={"checks": checks})
        else:
            emit_json(False, error=error("E-DOCTOR", "some checks failed", {"checks": checks}))
    else:
        marks = {"ok": "✓", "warn": "!", "error": "✗"}
        for c in checks:
            print(f"{marks[c['status']]} {c['check']:<13} {c['detail']}")
    return ExitCode.OK if ok else ExitCode.RUN_FAILED
