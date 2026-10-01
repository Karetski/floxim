"""The `python` node: call a function in a child process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from whisperwind import jsonschemas
from whisperwind.engine.nodes import AttemptResult, VisitContext
from whisperwind.engine.process import run_process
from whisperwind.engine.shell import MAX_OUTPUT_OBJECT

PYCALL = Path(__file__).resolve().parent.parent / "pycall.py"
LOG_TAIL = 64 * 1024


class PythonExecutor:
    default_on_resume = "restart"
    handles_timeout = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        runner = ctx.runner
        config = ctx.config
        assert ctx.attempt_dir is not None
        result_file = ctx.attempt_dir / "call-result.json"
        workdir = runner.node_workdir(ctx)
        request = {
            "call": config["call"],
            "args": config.get("args") or {},
            "ctx": {
                "run_id": runner.meta["id"],
                "node_id": ctx.node.id,
                "visit": ctx.visit,
                "artifacts_dir": str(runner.run_dir.artifacts_dir(ctx.node.id, ctx.visit)),
                "workdir": str(workdir),
            },
            "project_root": str(runner.config.root),
            "result_file": str(result_file),
        }
        interpreter = str(config.get("interpreter") or sys.executable)
        proc = await run_process(
            [interpreter, str(PYCALL)],
            cwd=workdir,
            env=runner.process_env(ctx),
            stdin=json.dumps(request),
            stdout_file=ctx.attempt_dir / "stdout.log",
            stderr_file=ctx.attempt_dir / "stderr.log",
            max_output=LOG_TAIL,
            timeout=runner.timeout_for(ctx.node, config),
            grace=runner.grace,
            stop=ctx.stop,
            redact=runner.redactor.text,
        )
        extra = {"stopped_by": proc.stopped_by} if proc.stopped_by else {}
        fields: dict[str, Any] = {"output": None}
        if proc.spawn_error is not None:
            return AttemptResult(
                "failed", fields, {"kind": "spawn_failed", "message": proc.spawn_error}
            )
        if proc.stop_reason == "timeout":
            message = f"timed out; stopped by {proc.stopped_by}"
            return AttemptResult(
                "timed_out", fields, {"kind": "timeout", "message": message}, extra=extra
            )
        if proc.stop_reason is not None:
            outcome = "cancelled" if proc.stop_reason == "cancel" else "interrupted"
            return AttemptResult(outcome, fields, None, extra=extra)
        try:
            reply = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            message = f"the call did not finish (exit code {proc.exit_code}): {proc.stderr[-500:]}"
            return AttemptResult("failed", fields, {"kind": "exception", "message": message})
        if not reply["ok"]:
            (ctx.attempt_dir / "traceback.txt").write_text(reply.get("traceback", ""))
            return AttemptResult("failed", fields, {"kind": "exception", "message": reply["error"]})
        output = reply["value"]
        if len(json.dumps(output)) > MAX_OUTPUT_OBJECT:
            return AttemptResult(
                "failed",
                fields,
                {"kind": "output_too_large", "message": "output is larger than 1 MiB"},
            )
        fields["output"] = output
        schema = runner.flow.schema_for(ctx.node, "output_schema")
        if schema is not None:
            error = jsonschemas.validation_error(jsonschemas.compile_schema(schema), output)
            if error is not None:
                return AttemptResult("schema_invalid", fields, {"kind": "schema", "message": error})
        return AttemptResult("succeeded", fields)
