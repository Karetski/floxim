"""The python node (spec §5.7)."""

import os
import sys
from pathlib import Path

from engine_support import Result, run_flow

TOOLS = """
import time

def coverage_ok(report, threshold, ctx):
    return {"ok": report["coverage"] >= threshold, "node": ctx.node_id, "visit": ctx.visit}

def boom():
    raise ValueError("bad input")

def not_json():
    return {1, 2}

def hang():
    time.sleep(60)

def plain(x):
    print("hello from the function")
    return x * 2
"""
ENV = {"PATH": os.environ["PATH"], "HOME": "/tmp"}


def call(tmp_path: Path, node: str) -> Result:
    flow = f"name: py\nnodes:\n  call:\n    type: python\n{node}"
    return run_flow(
        tmp_path,
        flow,
        files={"tools/__init__.py": "", "tools/checks_mod.py": TOOLS},
        virtual=False,
        environ=ENV,
        grace=0.5,
    )


def test_given_function_when_called_then_its_return_value_is_the_output(tmp_path: Path) -> None:
    # When
    result = call(
        tmp_path,
        """    call: tools.checks_mod:coverage_ok
    args:
      report: {coverage: 0.9}
      threshold: 0.8
    output_schema:
      type: object
      required: [ok]
      properties: {ok: {type: boolean}}
""",
    )

    # Then
    assert result.status == "succeeded", result.state["failure"]
    assert result.state["nodes"]["call"]["output"] == {"ok": True, "node": "call", "visit": 1}
    assert "tools.checks_mod" not in sys.modules


def test_given_function_without_ctx_when_called_then_it_gets_only_its_args(tmp_path: Path) -> None:
    # When
    result = call(tmp_path, "    call: tools.checks_mod:plain\n    args: {x: 21}\n")

    # Then
    assert result.state["nodes"]["call"]["output"] == 42
    stdout = result.run.attempt_dir("call", 1, 1) / "stdout.log"
    assert stdout.read_text() == "hello from the function\n"


def test_given_function_that_raises_when_called_then_failed_with_traceback_saved(
    tmp_path: Path,
) -> None:
    # When
    result = call(tmp_path, "    call: tools.checks_mod:boom\n")

    # Then
    node = result.state["nodes"]["call"]
    assert node["error"] == {"kind": "exception", "message": "ValueError: bad input"}
    traceback = (result.run.attempt_dir("call", 1, 1) / "traceback.txt").read_text()
    assert "raise ValueError" in traceback


def test_given_non_json_return_value_when_called_then_the_failure_says_so(tmp_path: Path) -> None:
    # When
    result = call(tmp_path, "    call: tools.checks_mod:not_json\n")

    # Then
    message = result.state["nodes"]["call"]["error"]["message"]
    assert "not JSON serializable" in message


def test_given_hanging_function_when_timeout_passes_then_it_is_stopped(tmp_path: Path) -> None:
    # When
    result = call(tmp_path, "    call: tools.checks_mod:hang\n    timeout: 1s\n")

    # Then
    assert result.state["nodes"]["call"]["outcome"] == "timed_out"


def test_given_output_not_matching_schema_when_called_then_schema_invalid(tmp_path: Path) -> None:
    # When
    result = call(
        tmp_path,
        "    call: tools.checks_mod:plain\n    args: {x: 1}\n    output_schema: {type: string}\n",
    )

    # Then
    assert result.state["nodes"]["call"]["outcome"] == "schema_invalid"
