"""Live contract tests against a real Codex CLI.

Marked `live` and excluded by default; run with FLOXIM_LIVE=1. They settle
what the offline suite cannot: signals, exit codes and the schema subset.
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
        os.environ.get("FLOXIM_LIVE") != "1" or shutil.which("codex") is None,
        reason="live tests need FLOXIM_LIVE=1 and the codex CLI",
    ),
]
ENV = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "OPENAI_API_KEY", "CODEX_HOME")}
FLOW = "name: live\ndefaults:\n  agent:\n    harness: codex\n    timeout: 5m\n    budget: {tokens: 200000}\nnodes:\n"


def test_thread_resume_and_strict_schema(tmp_path: Path) -> None:
    result = run_flow(
        tmp_path,
        FLOW
        + """  first:
    type: agent
    permissions: read-only
    prompt: Reply with the word ready.
    output_schema:
      type: object
      additionalProperties: false
      required: [word]
      properties: {word: {type: string}}
    next: second
  second:
    type: agent
    permissions: read-only
    session: {resume: first}
    prompt: Which word did you reply with?
""",
        virtual=False,
        environ=ENV,
    )
    assert result.status == "succeeded", result.state["failure"]
    nodes = result.state["nodes"]
    assert nodes["second"]["session_id"] == nodes["first"]["session_id"]


def test_timeout_stops_codex(tmp_path: Path) -> None:
    result = run_flow(
        tmp_path,
        FLOW + "  slow:\n    type: agent\n    timeout: 5s\n    prompt: Count slowly to 1000.\n",
        virtual=False,
        environ=ENV,
    )
    assert result.state["nodes"]["slow"]["outcome"] == "timed_out"
