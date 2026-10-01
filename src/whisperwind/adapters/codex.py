"""The Codex CLI adapter.

Drives `codex exec --json` with the prompt on stdin. Codex chooses the session
(thread) ID and announces it in `thread.started`. The outcome is judged from
`turn.completed` / `turn.failed`, never from the exit code alone. Checked
against codex-cli 0.147.0 (`codex exec --help`, 2026-09-29): `exec resume`
takes no --sandbox, -C or --add-dir, so sandbox, approval and writable roots
are always passed as `-c` config overrides, and the working directory is the
process's; `exec` has no fork command, so the adapter cannot fork.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

from whisperwind.adapters import (
    AdapterEvent,
    AgentRequest,
    AgentResult,
    Capabilities,
    ErrorInfo,
    Log,
    SessionStarted,
    Text,
    ToolCall,
    ToolResult,
    Usage,
    UsageUpdate,
)
from whisperwind.adapters.process import ProcessAdapter, StreamParser

SANDBOX = {"read-only": "read-only", "edit": "workspace-write", "full": "danger-full-access"}

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "network": {"type": "boolean", "description": "allow network access in workspace-write"},
        "profile": {"type": "string", "description": "a Codex config profile (-p)"},
        "ignore_user_config": {"type": "boolean", "description": "default true"},
        "extra_args": {"type": "array", "items": {"type": "string"}},
    },
}


def toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


class CodexAdapter(ProcessAdapter):
    name: ClassVar[str] = "codex"
    binary: ClassVar[str] = "codex"
    auth_env: ClassVar[tuple[str, ...]] = (
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
    )

    def __init__(self) -> None:
        super().__init__()
        self._version: str | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            structured_output=True,
            session_id="harness",
            resume=True,
            fork=False,
            interactive=True,
            cost_usd=False,
            tokens=True,
            budget_cap=False,
            turn_cap=False,
            tool_rules=False,
            streaming=True,
            effort=True,
            bare=False,
        )

    def options_schema(self) -> dict[str, Any]:
        return OPTIONS_SCHEMA

    def stdin(self, req: AgentRequest) -> str | None:
        if req.instructions:
            return f"# Instructions\n\n{req.instructions}\n\n# Task\n\n{req.prompt}"
        return req.prompt

    def command(self, req: AgentRequest) -> list[str]:
        options = req.options
        attempt_dir = Path(req.attempt_dir)
        argv = [self.command_path, "exec"]
        resuming = req.session.mode != "new" and req.session.resume_id
        if resuming:
            argv += ["resume", str(req.session.resume_id)]
        argv += ["--json", "--skip-git-repo-check"]
        argv += ["-c", f"sandbox_mode={toml_string(SANDBOX[req.permissions])}"]
        argv += ["-c", 'approval_policy="never"']
        if options.get("network"):
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if req.add_dirs:
            roots = ", ".join(toml_string(d) for d in req.add_dirs)
            argv += ["-c", f"sandbox_workspace_write.writable_roots=[{roots}]"]
        if options.get("ignore_user_config", True):
            argv.append("--ignore-user-config")
        if options.get("profile"):
            argv += ["-p", str(options["profile"])]
        if req.model:
            argv += ["-m", req.model]
        if req.effort:
            argv += ["-c", f"model_reasoning_effort={toml_string(req.effort)}"]
        if req.output_schema is not None:
            schema_file = attempt_dir / "output-schema.json"
            schema_file.write_text(json.dumps(req.output_schema))
            argv += ["--output-schema", str(schema_file)]
        argv += ["-o", str(attempt_dir / "last-message.txt")]
        if not resuming:
            argv += ["-C", req.cwd]
        argv.append("-")
        return argv

    def parser(self, req: AgentRequest) -> StreamParser:
        return CodexStream(req, self._version)

    async def run(self, req: AgentRequest, emit: Any) -> AgentResult:
        if self._version is None:
            probe = await self.probe()
            self._version = probe.version
        return await super().run(req, emit)

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None:
        return [self.command_path, "resume", session_id]


def normalize_usage(usage: dict[str, Any] | None) -> Usage:
    """Codex's input_tokens already include cached tokens, as Whisperwind's do."""
    usage = usage or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_output_tokens") or 0),
    )


class CodexStream(StreamParser):
    def __init__(self, req: AgentRequest, version: str | None) -> None:
        self.req = req
        self.version = version
        self.thread_id: str | None = req.session.resume_id if req.session.mode != "new" else None
        self.messages: list[str] = []
        self.usage = Usage()
        self.completed = False
        self.failure: str | None = None

    def feed(self, event: dict[str, Any]) -> list[AdapterEvent]:
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_id = str(event.get("thread_id"))
            return [SessionStarted(self.thread_id)]
        if kind == "turn.completed":
            self.completed = True
            turn = normalize_usage(event.get("usage"))
            self.usage = Usage(
                self.usage.input_tokens + turn.input_tokens,
                self.usage.cached_input_tokens + turn.cached_input_tokens,
                self.usage.output_tokens + turn.output_tokens,
                self.usage.reasoning_tokens + turn.reasoning_tokens,
            )
            return [UsageUpdate(self.usage, None)]
        if kind == "turn.failed":
            error = event.get("error") or {}
            self.failure = str(error.get("message") or "turn failed")
            return [Log("error", self.failure)]
        if kind == "error":
            self.failure = str(event.get("message") or "error")
            return [Log("error", self.failure)]
        if kind in ("item.started", "item.completed"):
            return self._item(event.get("item") or {}, done=kind == "item.completed")
        return []

    def _item(self, item: dict[str, Any], done: bool) -> list[AdapterEvent]:
        kind = item.get("type")
        if kind == "agent_message" and done:
            text = str(item.get("text") or "")
            self.messages.append(text)
            return [Text(text)]
        if kind == "command_execution":
            command = str(item.get("command") or "")
            if not done:
                return [ToolCall("command", command[:200])]
            ok = item.get("exit_code") == 0 and item.get("status") != "failed"
            return [ToolResult("command", ok, f"exit {item.get('exit_code')}: {command}"[:200])]
        if kind == "file_change" and done:
            paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
            return [
                ToolResult(
                    "file_change", item.get("status") != "failed", ", ".join(map(str, paths))[:200]
                )
            ]
        if kind == "mcp_tool_call" and not done:
            return [ToolCall(f"{item.get('server')}/{item.get('tool')}", "")]
        if kind == "web_search" and not done:
            return [ToolCall("web_search", str(item.get("query") or ""))]
        if kind == "error" and done:
            return [Log("error", str(item.get("message") or ""))]
        return []

    def finish(self, req: AgentRequest, exit_code: int | None, stderr_tail: str) -> AgentResult:
        last_file = Path(req.attempt_dir) / "last-message.txt"
        text = self.messages[-1] if self.messages else ""
        if not text and last_file.exists():
            text = last_file.read_text(encoding="utf-8", errors="replace")
        output = None
        if req.output_schema is not None and text:
            try:
                output = json.loads(text)
            except ValueError:
                match = re.search(r"\{.*\}", text, re.S)
                output = json.loads(match.group(0)) if match else None
        result = AgentResult(
            "succeeded",
            text=text,
            output=output,
            session_id=self.thread_id,
            usage=self.usage,
            harness_version=self.version,
            model=req.model,
        )
        detail = {"exit_code": exit_code}
        if self.failure is not None:
            result.outcome = "failed"
            result.error = ErrorInfo("harness_error", self.failure, detail)
        elif not self.completed:
            result.outcome = "failed"
            message = "the harness ended without turn.completed"
            if stderr_tail.strip():
                message += f": {stderr_tail.strip()[-300:]}"
            result.error = ErrorInfo("no_result", message, detail)
        return result
