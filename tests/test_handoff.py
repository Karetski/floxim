"""The handoff node."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_human_node import cli, latest, project

from whisperwind.validate import validate

FLOW = """name: hand
nodes:
  work:
    type: agent
    harness: fake
    prompt: Try it.
    next: take_over
  take_over:
    type: handoff
    from: work
    message: Over to you.
    next: after
  after:
    type: set
    vars: {back: true}
"""


def test_given_person_at_the_terminal_when_handoff_is_reached_then_the_session_opens_inline(
    tmp_path: Path,
) -> None:
    # When
    root = project(tmp_path, FLOW)
    result = cli(root, "run", "flow.yaml", "--on-wait", "prompt")

    # Then
    assert result.returncode == 0, result.stderr
    state = latest(root).read_state()
    take_over = state["nodes"]["take_over"]
    assert take_over["session_id"] == state["nodes"]["work"]["session_id"]
    assert take_over["exit_code"] == 0
    assert f"resuming fake session {take_over['session_id']}" in result.stdout
    assert "Over to you." in result.stderr


def test_given_a_pseudo_terminal_when_run_then_the_handoff_uses_it_by_default(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, FLOW)
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"no pseudo-terminal available here: {exc}")
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "XDG_CONFIG_HOME": str(root / "x")}

    # When
    process = subprocess.Popen(
        [sys.executable, "-m", "whisperwind", "run", "flow.yaml"],
        cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave,
    )  # fmt: skip
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                output += os.read(master, 4096)
            except OSError:
                break
    code = process.wait(10)
    os.close(master)

    # Then
    assert code == 0, output.decode(errors="replace")
    assert b"resuming fake session" in output


def test_given_no_terminal_when_handoff_is_reached_then_the_run_waits_for_whisperwind_handoff(
    tmp_path: Path,
) -> None:
    # Given
    root = project(tmp_path, FLOW)
    started = cli(root, "run", "flow.yaml", "--on-wait", "exit")
    pending = latest(root).read_state()["pending_human"]

    # When
    opened = cli(root, "handoff", "@last")

    # Then
    assert started.returncode == 4
    assert pending["take_over"]["kind"] == "handoff"
    assert opened.returncode == 0, opened.stderr
    assert "resuming fake session" in opened.stdout
    state = latest(root).read_state()
    assert state["status"] == "succeeded"
    assert state["nodes"]["take_over"]["exit_code"] == 0


def test_given_bad_handoff_sources_when_validated_then_they_are_rejected(tmp_path: Path) -> None:
    # Given
    harnesses = tmp_path / ".whisperwind" / "harnesses"
    harnesses.mkdir(parents=True)
    (harnesses / "plain.yaml").write_text(
        "name: plain\ncommand: [plain]\npermissions: {edit: []}\n"
        "stream:\n  result: {when: 'true', success: 'true'}\n"
    )
    (tmp_path / "flow.yaml").write_text(
        """name: bad
nodes:
  sh: {type: shell, run: echo, next: a}
  a: {type: handoff, from: sh, message: x, next: b}
  b: {type: handoff, from: nobody, message: x, next: c}
  c: {type: agent, harness: plain, prompt: Hi., next: d}
  d: {type: handoff, from: c, message: x}
"""
    )

    # When
    report = validate(tmp_path / "flow.yaml", implementation_gate=False)

    # Then
    found = sorted((p.pointer, p.code) for p in report.errors)
    assert found == [
        ("/nodes/a/from", "E-SCHEMA"),
        ("/nodes/b/from", "E-UNKNOWN-REF"),
        ("/nodes/d/from", "E-SCHEMA"),
    ]
