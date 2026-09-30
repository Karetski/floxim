"""Floxim-managed git worktrees (spec §6.9, ADR 0004).

A node with `workspace: worktree` runs in a worktree private to it; a named
worktree is shared by every node that names it in the run. Worktrees are
created on first use under `.floxim/worktrees/<run-id>/<name>`, recorded with
`workspace_created`, and reused by later visits and by resume. Floxim never
merges, pushes or deletes branches.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class WorkspaceSpec:
    name: str
    base: str | None
    branch: str | None
    keep: bool


def spec_for(node_id: str, value: Any) -> WorkspaceSpec | None:
    """The worktree a node asks for, or None for the shared run workdir."""
    if value is None or value == "shared":
        return None
    if value == "worktree":
        return WorkspaceSpec(node_id, None, None, True)
    return WorkspaceSpec(
        str(value["worktree"]),
        value.get("base"),
        value.get("branch"),
        bool(value.get("keep", True)),
    )


def git(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=dict(env), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def head_commit(workdir: Path, env: Mapping[str, str]) -> str | None:
    try:
        return git(["rev-parse", "HEAD"], workdir, env)
    except (WorkspaceError, OSError):
        return None


def create(
    spec: WorkspaceSpec,
    *,
    run_id: str,
    root: Path,
    workdir: Path,
    default_base: str | None,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Create the worktree; returns the `workspace_created` data."""
    path = root / ".floxim" / "worktrees" / run_id / spec.name
    branch = spec.branch or f"floxim/{run_id}/{spec.name}"
    base = spec.base or default_base or "HEAD"
    base_commit = git(["rev-parse", "--verify", f"{base}^{{commit}}"], workdir, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path), base_commit], workdir, env)
    return {
        "name": spec.name,
        "path": str(path),
        "branch": branch,
        "base_commit": base_commit,
        "keep": spec.keep,
    }


def recreate(recorded: Mapping[str, Any], workdir: Path, env: Mapping[str, str]) -> None:
    """Put back a recorded worktree that was deleted (`--recreate-workspaces`)."""
    git(["worktree", "prune"], workdir, env)
    git(["worktree", "add", str(recorded["path"]), str(recorded["branch"])], workdir, env)


def remove(recorded: Mapping[str, Any], workdir: Path, env: Mapping[str, str]) -> None:
    git(["worktree", "remove", "--force", str(recorded["path"])], workdir, env)
