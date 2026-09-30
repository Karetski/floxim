"""Run IDs and run arguments (spec §7.1)."""

from __future__ import annotations

import datetime
import secrets
from pathlib import Path

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # base32, lower case


class RunNotFound(Exception):
    pass


class AmbiguousRun(Exception):
    def __init__(self, arg: str, matches: list[str]) -> None:
        super().__init__(f"{arg!r} matches several runs: {', '.join(matches[:5])}")
        self.matches = matches


def new_run_id(flow_name: str, now: datetime.datetime, suffix: str | None = None) -> str:
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = suffix or "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"{stamp}-{flow_name}-{suffix}"


def flow_of(run_id: str) -> str:
    return run_id.split("-", 1)[1].rsplit("-", 1)[0]


def list_run_ids(runs_dir: Path) -> list[str]:
    """Run IDs, oldest first (IDs sort by start time)."""
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if (p / "events.jsonl").exists())


def resolve_run(runs_dir: Path, arg: str) -> str:
    """A full run ID from an ID, a unique prefix or suffix, `@last` or `@last:<flow>`."""
    ids = list_run_ids(runs_dir)
    if arg == "@last" or arg.startswith("@last:"):
        flow = arg.partition(":")[2]
        candidates = [i for i in ids if not flow or flow_of(i) == flow]
        if not candidates:
            raise RunNotFound(f"no runs{' of flow ' + flow if flow else ''} in {runs_dir}")
        return candidates[-1]
    if arg in ids:
        return arg
    matches = [i for i in ids if i.startswith(arg) or i.endswith(arg)]
    if not matches:
        raise RunNotFound(f"no run matches {arg!r} in {runs_dir}")
    if len(matches) > 1:
        raise AmbiguousRun(arg, matches)
    return matches[0]
