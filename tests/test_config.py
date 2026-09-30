"""Project root discovery and configuration."""

from pathlib import Path

from floxim.config import find_project_root, load_config
from floxim.validate import validate


def test_given_floxim_dir_above_when_root_searched_then_it_wins_over_git(tmp_path: Path) -> None:
    # Given
    (tmp_path / ".git").mkdir()
    (tmp_path / "proj" / ".floxim").mkdir(parents=True)
    (tmp_path / "proj" / "sub").mkdir()

    # When / Then
    assert find_project_root(tmp_path / "proj" / "sub") == (tmp_path / "proj").resolve()


def test_given_only_git_when_root_searched_then_the_repository_root_is_used(tmp_path: Path) -> None:
    # Given
    (tmp_path / ".git").mkdir()
    (tmp_path / "a" / "b").mkdir(parents=True)

    # When / Then
    assert find_project_root(tmp_path / "a" / "b") == tmp_path.resolve()


def test_given_user_and_project_config_when_loaded_then_project_overrides_user(
    tmp_path: Path, isolated_user_config: Path
) -> None:
    # Given
    isolated_user_config.parent.mkdir(parents=True)
    isolated_user_config.write_text("env_passthrough: [A]\nretention: {keep_days: 7}\n")
    (tmp_path / ".floxim").mkdir()
    (tmp_path / ".floxim" / "config.yaml").write_text("env_passthrough: [B]\n")

    # When
    config, problems = load_config(tmp_path)

    # Then
    assert problems == []
    assert config["env_passthrough"] == ["B"]
    assert config["retention"] == {"keep_days": 7}
    assert config["flow_paths"] == ["flows"]


def test_given_unknown_config_key_when_loaded_then_reports_it_with_position(tmp_path: Path) -> None:
    # Given
    (tmp_path / ".floxim").mkdir()
    (tmp_path / ".floxim" / "config.yaml").write_text("runs_dir: x\nflow_path: [flows]\n")

    # When
    _, problems = load_config(tmp_path)

    # Then
    assert [(p.code, p.line, p.column) for p in problems] == [("E-UNKNOWN-KEY", 2, 1)]


def test_given_risky_commands_overridden_when_validated_then_the_lint_follows_config(
    tmp_path: Path,
) -> None:
    # Given
    (tmp_path / ".floxim").mkdir()
    (tmp_path / ".floxim" / "config.yaml").write_text("risky_commands: ['\\bdeploy\\b']\n")
    flow = tmp_path / "flow.yaml"
    flow.write_text(
        "name: r\nnodes:\n  push: {type: shell, run: git push, next: ship}\n"
        "  ship: {type: shell, run: ./deploy prod}\n"
    )
    config, _ = load_config(tmp_path)

    # When
    report = validate(flow, config=config, workdir=tmp_path, implementation_gate=False)

    # Then
    risky = [p for p in report.problems if p.code == "W-NO-HUMAN-BEFORE-RISKY"]
    assert [p.pointer for p in risky] == ["/nodes/ship"]


def test_given_worktree_outside_git_when_validated_then_reports_workspace_no_git(
    tmp_path: Path,
) -> None:
    # Given
    flow = tmp_path / "flow.yaml"
    flow.write_text("name: w\nnodes:\n  a: {type: shell, run: make, workspace: worktree}\n")

    # When
    report = validate(flow, workdir=tmp_path, implementation_gate=False)

    # Then
    assert [(p.code, p.pointer) for p in report.errors] == [
        ("E-WORKSPACE-NO-GIT", "/nodes/a/workspace")
    ]
