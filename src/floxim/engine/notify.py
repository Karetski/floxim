"""The `notify` node: a command or a webhook, fire and forget (spec §5.11)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from floxim.engine.nodes import AttemptResult, VisitContext

COMMAND_TIMEOUT_S = 30.0
WEBHOOK_TIMEOUT_S = 10.0


class NotifyExecutor:
    default_on_resume = "restart"

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        config = ctx.config
        message = str(config["message"])
        if "command" in config:
            problem = await self._command(ctx, str(config["command"]), message)
        else:
            problem = await asyncio.get_running_loop().run_in_executor(
                None, _post, dict(config["webhook"])
            )
        if problem is None:
            return AttemptResult("succeeded", {})
        if config.get("required", False):
            return AttemptResult.failed("harness_error", f"notification failed: {problem}")
        ctx.runner.emit(
            "warning",
            {"code": "W-NOTIFY-FAILED", "message": problem},
            node=ctx.node.id,
            visit=ctx.visit,
        )
        return AttemptResult("succeeded", {})

    async def _command(self, ctx: VisitContext, command: str, message: str) -> str | None:
        env = ctx.runner.process_env(ctx)
        env["FLOXIM_MESSAGE"] = message
        try:
            process = await asyncio.create_subprocess_exec(
                "sh",
                "-c",
                command,
                cwd=str(ctx.runner.workdir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return str(exc)
        try:
            _, err = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return f"the command timed out after {COMMAND_TIMEOUT_S:g}s"
        if process.returncode != 0:
            detail = err.decode("utf-8", errors="replace").strip()[-300:]
            return f"exit code {process.returncode}" + (f": {detail}" if detail else "")
        return None


def _post(webhook: dict[str, Any]) -> str | None:
    body = json.dumps(webhook.get("body", {})).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **{k: str(v) for k, v in (webhook.get("headers") or {}).items()},
    }
    request = urllib.request.Request(
        str(webhook["url"]), data=body, headers=headers, method=str(webhook.get("method", "POST"))
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as response:
            status = response.status
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return str(getattr(exc, "reason", exc))
    return None if 200 <= status < 300 else f"HTTP {status}"
