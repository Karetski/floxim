"""Workspaces and artifacts (spec §6.9, §6.10)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from engine_support import run_flow

from floxim.clock import Clock
from floxim.config import load_config
from floxim.engine.runner import ResumeRefused, Runner
from floxim.store.events import read_log
from floxim.store.ids import resolve_run
from floxim.store.rundir import RunDir

GIT_ENV = {
    "PATH": os.environ["PATH"],
    "HOME": "/tmp",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README").write_text("hello\n")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


SHARED_TREE = """
name: trees
nodes:
  write:
    type: shell
    workspace: {worktree: feature}
    run: echo made > made.txt
    next: read
  read:
    type: shell
    workspace: {worktree: feature}
    run: cat made.txt
    next: outside
  outside:
    type: shell
    run: cat "${{ nodes.write.workspace.path }}/made.txt"
    next: artifact
  artifact:
    type: shell
    run: echo report > "$FLOXIM_ARTIFACTS_DIR/report.txt"
"""


def test_given_named_worktree_when_two_nodes_use_it_then_they_share_its_files(repo: Path) -> None:
    # When
    result = run_flow(repo, SHARED_TREE, virtual=False, environ=GIT_ENV)

    # Then
    assert result.status == "succeeded", result.state["failure"]
    nodes = result.state["nodes"]
    assert nodes["read"]["stdout"] == "made\n"
    assert nodes["outside"]["stdout"] == "made\n"
    created = [e for e in result.events if e["type"] == "workspace_created"]
    assert len(created) == 1
    workspace = nodes["write"]["workspace"]
    assert workspace["branch"] == f"floxim/{result.run.id}/feature"
    assert Path(workspace["path"]) == repo / ".floxim" / "worktrees" / result.run.id / "feature"
    assert created[0]["data"]["base_commit"] == _git(repo, "rev-parse", "HEAD")
    assert nodes["outside"]["workspace"] == {"path": str(repo), "branch": None}
    report = Path(nodes["artifact"]["artifacts_dir"]) / "report.txt"
    assert report.read_text() == "report\n"
    assert not (repo / "made.txt").exists()


def test_given_keep_false_when_run_succeeds_then_the_worktree_is_removed(repo: Path) -> None:
    # Given
    flow = """
name: temp
nodes:
  work:
    type: shell
    workspace: {worktree: scratch, keep: false, branch: "scratch-${{ run.id }}"}
    run: git status --short
"""

    # When
    result = run_flow(repo, flow, virtual=False, environ=GIT_ENV)

    # Then
    assert result.status == "succeeded"
    path = Path(result.state["nodes"]["work"]["workspace"]["path"])
    assert not path.exists()
    assert "workspace_removed" in result.types()
    assert f"scratch-{result.run.id}" in _git(repo, "branch", "--list")  # branches are kept


def _cli(
    project: Path, *args: str, crash_at: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**GIT_ENV, "XDG_CONFIG_HOME": str(project / "xdg")}
    if crash_at:
        env["FLOXIM_TEST_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, "-m", "floxim", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )


def test_given_crash_after_worktree_creation_when_resumed_then_the_same_worktree_is_reused(
    repo: Path,
) -> None:
    # Given
    (repo / ".floxim").mkdir()
    (repo / "flow.yaml").write_text(SHARED_TREE)
    assert _cli(repo, "run", "flow.yaml", crash_at="workspace_created:1").returncode == 137

    # When
    resumed = _cli(repo, "resume", "@last")

    # Then
    assert resumed.returncode == 0, resumed.stderr
    runs = repo / ".floxim" / "runs"
    events = read_log(RunDir(runs / resolve_run(runs, "@last")).events).events
    assert [e["type"] for e in events].count("workspace_created") == 1


COMMITTING_TREE = SHARED_TREE.replace(
    "run: echo made > made.txt",
    "run: echo made > made.txt && git add made.txt && git commit -qm made",
)


def test_given_deleted_worktree_when_resumed_then_it_is_refused_unless_recreated(
    repo: Path,
) -> None:
    # Given: the run stops after `write` committed its file on the worktree's branch
    (repo / ".floxim").mkdir()
    (repo / "flow.yaml").write_text(COMMITTING_TREE)
    assert _cli(repo, "run", "flow.yaml", crash_at="route_taken:1").returncode == 137
    runs = repo / ".floxim" / "runs"
    run = RunDir(runs / resolve_run(runs, "@last"))
    tree = Path(run.read_state()["workspaces"]["feature"]["path"])
    subprocess.run(["rm", "-rf", str(tree)], check=True)
    config, _ = load_config(repo)

    # When / Then
    with pytest.raises(ResumeRefused, match="E-WORKSPACE-MISSING"):
        Runner(run, config, Clock()).check_resumable()
    resumed = _cli(repo, "resume", "@last", "--recreate-workspaces")
    assert resumed.returncode == 0, resumed.stderr
    assert (tree / "made.txt").read_text() == "made\n"
