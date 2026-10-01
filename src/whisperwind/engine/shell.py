"""The `shell` node: run a command."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from whisperwind import jsonschemas
from whisperwind.engine.nodes import AttemptResult, VisitContext
from whisperwind.engine.process import ProcessResult, run_process
from whisperwind.units import parse_size

MAX_OUTPUT_OBJECT = 1024 * 1024  # serialized `output` kept in state


def shell_argv(config: dict[str, Any], env_path: str | None) -> list[str]:
    if "args" in config:
        return [str(a) for a in config["args"]]
    interpreter = config.get("shell")
    if interpreter is None:
        interpreter = "bash" if shutil.which("bash", path=env_path) else "sh"
    name = Path(interpreter).name
    flags = ["-eo", "pipefail"] if name in ("bash", "zsh", "ksh") else ["-e"]
    return [interpreter, *flags, "-c", str(config["run"])]


class ShellExecutor:
    default_on_resume = "restart"
    handles_timeout = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        runner = ctx.runner
        config = ctx.config
        assert ctx.attempt_dir is not None
        env = runner.process_env(ctx)
        workdir = runner.node_workdir(ctx)
        cwd = workdir / str(config["cwd"]) if config.get("cwd") else workdir
        stdout_file = ctx.attempt_dir / "stdout.log"
        stderr_file = ctx.attempt_dir / "stderr.log"
        max_output = parse_size(config.get("max_output", "64KiB"))
        result = await run_process(
            shell_argv(config, env.get("PATH")),
            cwd=cwd,
            env=env,
            stdin=config.get("stdin"),
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            max_output=max_output,
            timeout=runner.timeout_for(ctx.node, config),
            grace=runner.grace,
            stop=ctx.stop,
            redact=runner.redactor.text,
        )
        return _result(ctx, result, stdout_file, stderr_file)


def _result(
    ctx: VisitContext, proc: ProcessResult, stdout_file: Path, stderr_file: Path
) -> AttemptResult:
    config = ctx.config
    fields: dict[str, Any] = {
        "exit_code": proc.exit_code,
        "signal": proc.signal,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "stdout_truncated": proc.stdout_truncated,
        "stderr_truncated": proc.stderr_truncated,
        "output": None,
    }
    extra = {"stopped_by": proc.stopped_by} if proc.stopped_by else {}
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
    ok_codes = config.get("ok_codes", [0])
    if proc.exit_code not in ok_codes:
        what = (
            f"exit code {proc.exit_code}" if proc.exit_code is not None else f"signal {proc.signal}"
        )
        return AttemptResult("failed", fields, {"kind": "exit_code", "message": what})
    mode = config.get("output", "none")
    if mode == "text":
        fields["output"] = stdout_file.read_text(encoding="utf-8", errors="replace")
    elif mode == "json":
        try:
            output = json.loads(stdout_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return AttemptResult(
                "failed", fields, {"kind": "output_parse", "message": f"stdout is not JSON: {exc}"}
            )
        if len(json.dumps(output)) > MAX_OUTPUT_OBJECT:
            return AttemptResult(
                "failed",
                fields,
                {"kind": "output_too_large", "message": "output is larger than 1 MiB"},
            )
        fields["output"] = output
        schema = ctx.runner.flow.schema_for(ctx.node, "output_schema")
        if schema is not None:
            error = jsonschemas.validation_error(jsonschemas.compile_schema(schema), output)
            if error is not None:
                return AttemptResult("schema_invalid", fields, {"kind": "schema", "message": error})
    return AttemptResult("succeeded", fields)
