"""Live contract tests against a real Claude Code install.

They spend money, so they are marked `live`, excluded by default, and run only
with WHISPERWIND_LIVE=1: `WHISPERWIND_LIVE=1 uv run pytest -m live tests/live`. Each
call is capped with --max-budget-usd through a node budget.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from engine_support import run_flow

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("WHISPERWIND_LIVE") != "1" or shutil.which("claude") is None,
        reason="live tests need WHISPERWIND_LIVE=1 and the claude CLI",
    ),
]
ENV = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "ANTHROPIC_API_KEY")}


def live(tmp_path: Path, nodes: str):  # type: ignore[no-untyped-def]
    flow = (
        "name: live\ndefaults:\n  agent:\n    harness: claude\n    model: haiku\n"
        "    budget: {usd: 0.05}\n    timeout: 3m\nnodes:\n" + nodes
    )
    return run_flow(tmp_path, flow, virtual=False, environ=ENV)


def test_session_id_resume_and_schema_output(tmp_path: Path) -> None:
    result = live(
        tmp_path,
        """  first:
    type: agent
    permissions: read-only
    prompt: Reply with the word ready.
    output_schema: {type: object, required: [word], properties: {word: {type: string}}}
    next: second
  second:
    type: agent
    permissions: read-only
    session: {resume: first}
    prompt: What word did you reply with? Answer in one word.
""",
    )
    assert result.status == "succeeded", result.state["failure"]
    nodes = result.state["nodes"]
    assert nodes["first"]["output"]["word"].lower().startswith("ready")
    assert nodes["second"]["session_id"] == nodes["first"]["session_id"]
    assert nodes["second"]["cost_usd"] < nodes["first"]["cost_usd"] + 0.05


def test_read_only_profile_cannot_write(tmp_path: Path) -> None:
    result = live(
        tmp_path,
        "  write:\n    type: agent\n    permissions: read-only\n"
        "    prompt: Create a file named created.txt containing hi.\n",
    )
    assert not (tmp_path / "created.txt").exists()
    assert result.state["nodes"]["write"]["outcome"] in ("succeeded", "failed")


def test_cancel_within_grace(tmp_path: Path) -> None:
    result = live(
        tmp_path,
        "  slow:\n    type: agent\n    timeout: 5s\n"
        "    prompt: Count slowly from 1 to 500, one number per line.\n",
    )
    assert result.state["nodes"]["slow"]["outcome"] == "timed_out"
