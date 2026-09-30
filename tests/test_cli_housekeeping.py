"""init, adapters, gc, doctor, global flags, and the published CLI output schemas
(spec §9.1, §9.3, §7.7)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import fastjsonschema
import pytest
from test_human_node import cli, latest, project

from arcflow.store.lock import STALE_AFTER_S

QUICK = "name: quick\nnodes:\n  a: {type: set, vars: {x: 1}}\n"
ASK = "name: ask\nnodes:\n  ask: {type: human, message: Go?, choices: [go, stop]}\n"


def js(result: subprocess.CompletedProcess[str]) -> Any:
    return json.loads(result.stdout)


def test_given_empty_project_when_init_runs_then_config_gitignore_and_a_valid_example_exist(
    tmp_path: Path,
) -> None:
    # When
    first = js(cli(tmp_path, "init", "--json"))["data"]
    second = js(cli(tmp_path, "init", "--json"))["data"]

    # Then
    assert sorted(Path(p).name for p in first["created"]) == [
        ".gitignore",
        "config.yaml",
        "hello.yaml",
    ]
    assert second["created"] == [] and len(second["kept"]) == 3
    assert (tmp_path / ".arcflow" / ".gitignore").read_text() == "runs/\nworktrees/\n"
    assert cli(tmp_path, "validate", "flows/hello.yaml").returncode == 0
    assert (
        cli(tmp_path, "run", "flows/hello.yaml", "--on-wait", "prompt", stdin="finish\n").returncode
        == 0
    )


def test_given_global_flags_when_used_before_or_after_the_command_then_they_apply(
    tmp_path: Path,
) -> None:
    # Given: a project elsewhere, and a config file that is not the project's
    (tmp_path / "proj").mkdir()
    elsewhere = project(tmp_path / "proj", QUICK)
    extra = tmp_path / "extra.yaml"
    extra.write_text("runs_dir: other-runs\n")

    # When
    cli(tmp_path, "--project", str(elsewhere), "run", str(elsewhere / "flow.yaml"))
    cli(
        tmp_path,
        "run",
        str(elsewhere / "flow.yaml"),
        "--project",
        str(elsewhere),
        "--config",
        str(extra),
    )
    quiet = cli(
        tmp_path, "run", str(elsewhere / "flow.yaml"), "--project", str(elsewhere), "--quiet"
    )

    # Then
    assert len(list((elsewhere / ".arcflow" / "runs").iterdir())) == 2
    assert len(list((elsewhere / "other-runs").iterdir())) == 1
    assert quiet.stderr == ""


def test_given_as_flag_when_responding_then_it_names_the_responder(tmp_path: Path) -> None:
    root = project(tmp_path, ASK)
    assert cli(root, "run", "flow.yaml", "--on-wait", "exit").returncode == 4
    assert (
        cli(root, "--as", "grace", "respond", "@last", "--choice", "go", "--no-continue").returncode
        == 0
    )
    assert latest(root).read_state()["pending_human"] == {}
    events = latest(root).events.read_text()
    assert '"responder":"grace"' in events


def test_given_stub_harness_when_adapters_probed_then_versions_and_sources_are_listed(
    tmp_path: Path,
) -> None:
    # Given
    stub = tmp_path / "claude-stub"
    stub.write_text("#!/bin/sh\necho '2.1.300 (Claude Code)'\n")
    stub.chmod(0o755)
    root = project(
        tmp_path,
        QUICK,
        config=f"harnesses:\n  claude: {{command: {stub}, tested_versions: '<2.1.290'}}\n",
    )

    # When
    found = {
        a["name"]: a for a in js(cli(root, "adapters", "--probe", "--json"))["data"]["adapters"]
    }

    # Then
    assert {"claude", "codex", "fake"} <= set(found)
    assert found["fake"]["source"] == "built-in"
    assert found["claude"]["probe"]["version"] == "2.1.300"
    assert found["claude"]["probe"]["in_tested_range"] is False
    assert found["claude"]["capabilities"]["session_id"] == "caller"


def test_given_old_finished_and_waiting_runs_when_collected_then_only_finished_ones_go(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, QUICK)
    (root / "ask.yaml").write_text(ASK)
    assert cli(root, "run", "flow.yaml").returncode == 0
    assert cli(root, "run", "ask.yaml", "--on-wait", "exit").returncode == 4
    time.sleep(1.1)

    # When
    dry = js(cli(root, "gc", "--older-than", "1s", "--dry-run", "--json"))["data"]
    real = js(cli(root, "gc", "--older-than", "1s", "--json"))["data"]

    # Then
    assert dry["dry_run"] and len(dry["removed"]) == 1
    assert real["removed"] == dry["removed"]
    remaining = [p.name for p in (root / ".arcflow" / "runs").iterdir()]
    assert len(remaining) == 1 and "ask" in remaining[0]


def test_given_damaged_run_when_doctor_runs_then_it_repairs_what_is_safe(tmp_path: Path) -> None:
    # Given: a torn last line, a stale lock and a stale state.json
    root = project(tmp_path, QUICK)
    assert cli(root, "run", "flow.yaml").returncode == 0
    run = latest(root)
    with open(run.events, "ab") as handle:
        handle.write(b'{"v":1,"seq":99,"type":"vis')
    run.lock.write_text(
        json.dumps({"pid": 1, "host": "elsewhere", "heartbeat_at": "2020-01-01T00:00:00.000Z"})
    )
    run.state_json.write_text("{}")

    # When
    report = js(cli(root, "doctor", "@last", "--json"))["data"]["checks"]

    # Then
    statuses = {c["check"]: c["status"] for c in report}
    assert statuses == {"events": "warn", "state": "warn", "lock": "warn"}
    assert not run.lock.exists()
    assert run.read_state()["status"] == "succeeded"
    assert STALE_AFTER_S > 0


def test_given_corrupt_log_when_doctor_runs_then_it_reports_and_truncates_on_request(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, QUICK)
    assert cli(root, "run", "flow.yaml").returncode == 0
    run = latest(root)
    lines = run.events.read_text().splitlines()
    run.events.write_text("\n".join([lines[0], "garbage", *lines[1:]]) + "\n")

    # When
    report = cli(root, "doctor", "@last")
    repaired = cli(root, "doctor", "@last", "--truncate")

    # Then
    assert report.returncode == 1 and "--truncate" in report.stdout
    assert repaired.returncode == 0
    assert (run.path / "events.jsonl.corrupt").exists()
    assert run.read_state()["last_seq"] == 1


# -- the CLI contract -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cli_schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("schema")
    document = js(cli(root, "schema", "cli"))
    assert set(document["definitions"]) >= {"run", "status", "list", "validate", "gc", "doctor"}
    assert isinstance(document, dict)
    return document


def check(schema: dict[str, Any], command: str, output: Any) -> None:
    validator = fastjsonschema.compile(
        {**schema["definitions"][command], "definitions": schema["definitions"]}
    )
    validator(output)


def test_given_every_command_when_run_with_json_then_its_output_matches_the_published_schema(
    tmp_path: Path, cli_schema: dict[str, Any]
) -> None:
    # Given
    root = project(tmp_path, QUICK)
    (root / "ask.yaml").write_text(ASK)
    (root / "flows").mkdir()
    (root / "flows" / "q.yaml").write_text(QUICK)

    # When / Then
    cases = [
        ("validate", ["validate", "flow.yaml"]),
        ("validate", ["validate", "missing.yaml"]),
        ("schema", ["schema", "flow"]),
        ("run", ["run", "flow.yaml"]),
        ("run", ["run", "ask.yaml", "--on-wait", "exit"]),
        ("status", ["status"]),
        ("status <run>", ["status", "@last"]),
        ("list", ["list"]),
        ("wait", ["wait", "@last"]),
        ("artifacts", ["artifacts", "@last"]),
        ("flows", ["flows"]),
        ("graph", ["graph", "flow.yaml"]),
        ("respond", ["respond", "@last", "--choice", "maybe"]),
        ("respond", ["respond", "@last", "--choice", "go", "--no-continue"]),
        ("resume", ["resume", "@last", "--on-wait", "exit"]),
        ("resume --due", ["resume", "--due"]),
        ("cancel", ["cancel", "@last"]),
        ("run --detach", ["run", "flow.yaml", "--detach"]),
        ("init", ["init"]),
        ("adapters", ["adapters"]),
        ("gc", ["gc", "--dry-run"]),
        ("doctor", ["doctor", "@last"]),
        ("adapter test", ["adapter", "test", "claude"]),
    ]
    for command, args in cases:
        output = js(cli(root, *args, "--json"))
        check(cli_schema, command, output)
