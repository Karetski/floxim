"""The Claude Code adapter.

Drives `claude -p` with `--output-format stream-json`. Floxim chooses session
IDs, so `session_started` is known before the process starts. The outcome is
judged from the final `result` event, never from the exit code alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from floxim.adapters import (
    AdapterEvent,
    AgentRequest,
    AgentResult,
    Capabilities,
    Denial,
    ErrorInfo,
    Log,
    PermissionDenied,
    SessionStarted,
    Text,
    ToolCall,
    ToolResult,
    Usage,
    UsageUpdate,
)
from floxim.adapters.process import ProcessAdapter, StreamParser

# Tools that change files; denied in the read-only profile.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
# Nested orchestration is denied by default.
ALWAYS_DENIED = ("Workflow",)


def permission_flags(profile: str) -> list[str]:
    """The permission profiles as Claude Code flags.

    read-only: dontAsk mode allows reads and pre-approved tools and denies
    anything that would prompt; file-editing tools are denied outright.
    edit: acceptEdits approves file edits and common filesystem commands;
    other commands would prompt, and prompts are denied.
    full: bypassPermissions.
    """
    if profile == "read-only":
        return ["--permission-mode", "dontAsk", "--permission-prompts", "none"]
    if profile == "edit":
        return ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    return ["--permission-mode", "bypassPermissions"]


class ClaudeAdapter(ProcessAdapter):
    name: ClassVar[str] = "claude"
    binary: ClassVar[str] = "claude"
    auth_env: ClassVar[tuple[str, ...]] = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )

    def capabilities(self) -> Capabilities:
        return Capabilities(
            structured_output=True,
            session_id="caller",
            resume=True,
            fork=True,
            interactive=True,
            cost_usd=True,
            tokens=True,
            budget_cap=True,
            turn_cap=True,
            tool_rules=True,
            permission_hook=False,
            streaming=True,
            effort=True,
            bare=True,
        )

    def command(self, req: AgentRequest) -> list[str]:
        argv = [self.command_path, "-p"]
        if req.bare:
            argv.append("--bare")
        argv += ["--output-format", "stream-json", "--verbose"]
        session = req.session
        if session.mode == "new":
            argv += ["--session-id", str(session.new_id)]
        else:
            argv += ["--resume", str(session.resume_id)]
            if session.mode == "fork":
                argv += ["--fork-session", "--session-id", str(session.new_id)]
        if req.output_schema is not None:
            argv += ["--json-schema", json.dumps(req.output_schema)]
        if req.model:
            argv += ["--model", req.model]
        if req.effort:
            argv += ["--effort", req.effort]
        if req.max_turns is not None:
            argv += ["--max-turns", str(req.max_turns)]
        if req.budget_usd is not None:
            argv += ["--max-budget-usd", f"{max(req.budget_usd, 0.0):.4f}"]
        argv += permission_flags(req.permissions)
        if req.allow_tools:
            argv += ["--allowedTools", *req.allow_tools]
        denied = [*ALWAYS_DENIED, *req.deny_tools]
        if req.permissions == "read-only":
            denied += list(EDIT_TOOLS)
        argv += ["--disallowedTools", *dict.fromkeys(denied)]
        if req.instructions is not None:
            path = Path(req.attempt_dir) / "instructions.md"
            path.write_text(req.instructions)
            argv += ["--append-system-prompt-file", str(path)]
        for directory in req.add_dirs:
            argv += ["--add-dir", directory]
        return argv

    def early_events(self, req: AgentRequest) -> list[AdapterEvent]:
        session = req.session
        known = session.resume_id if session.mode == "resume" else session.new_id
        return [SessionStarted(known)] if known else []

    def parser(self, req: AgentRequest) -> StreamParser:
        return ClaudeStream(req)

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None:
        return [self.command_path, "--resume", session_id]


def normalize_usage(usage: dict[str, Any] | None) -> Usage:
    """Claude's input_tokens exclude cache reads and writes; Floxim's include them."""
    usage = usage or {}
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    details = usage.get("output_tokens_details") or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0) + cache_read + cache_write,
        cached_input_tokens=cache_read,
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(details.get("thinking_tokens") or 0),
    )


class ClaudeStream(StreamParser):
    def __init__(self, req: AgentRequest) -> None:
        self.req = req
        self.version: str | None = None
        self.model: str | None = None
        self.session_id: str | None = None
        self.result: dict[str, Any] | None = None
        self.tools: dict[str, str] = {}  # tool_use_id → tool name
        self.step_usage: dict[str, dict[str, Any]] = {}  # message id → usage
        self.texts: list[str] = []
        self.denials: list[Denial] = []

    def feed(self, event: dict[str, Any]) -> list[AdapterEvent]:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.version = event.get("claude_code_version")
            self.model = event.get("model")
            self.session_id = event.get("session_id") or self.session_id
            return []
        if kind == "assistant":
            return self._assistant(event)
        if kind == "user":
            return self._tool_results(event)
        if kind == "permission_denied" or (
            kind == "system" and event.get("subtype") == "permission_denied"
        ):
            tool = str(event.get("tool_name") or event.get("tool") or "?")
            reason = str(event.get("reason") or event.get("message") or "denied")
            self.denials.append(Denial(tool, reason))
            return [PermissionDenied(tool, reason)]
        if kind == "system" and event.get("subtype") == "api_retry":
            return [Log("warning", f"API retry: {event.get('error') or event.get('message', '')}")]
        if kind == "result":
            self.result = event
            return [UsageUpdate(normalize_usage(event.get("usage")), self._delta(event))]
        return []

    def _assistant(self, event: dict[str, Any]) -> list[AdapterEvent]:
        message = event.get("message") or {}
        out: list[AdapterEvent] = []
        for block in message.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                self.texts.append(block["text"])
                out.append(Text(block["text"]))
            elif block.get("type") == "tool_use":
                name = str(block.get("name"))
                self.tools[str(block.get("id"))] = name
                summary = json.dumps(block.get("input"), ensure_ascii=False)[:200]
                out.append(ToolCall(name, summary))
        if message.get("id") and message.get("usage"):
            self.step_usage[message["id"]] = message["usage"]
            total = Usage()
            for step in self.step_usage.values():
                usage = normalize_usage(step)
                total = Usage(
                    total.input_tokens + usage.input_tokens,
                    total.cached_input_tokens + usage.cached_input_tokens,
                    total.output_tokens + usage.output_tokens,
                    total.reasoning_tokens + usage.reasoning_tokens,
                )
            out.append(UsageUpdate(total, None))
        return out

    def _tool_results(self, event: dict[str, Any]) -> list[AdapterEvent]:
        out: list[AdapterEvent] = []
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                name = self.tools.get(str(block.get("tool_use_id")), "?")
                content = block.get("content")
                summary = content if isinstance(content, str) else json.dumps(content)[:200]
                out.append(ToolResult(name, not block.get("is_error", False), str(summary)[:200]))
        return out

    def _delta(self, event: dict[str, Any]) -> float | None:
        total = event.get("total_cost_usd")
        if total is None:
            return None
        previous = self.req.session.previous_cost_usd if self.req.session.mode != "new" else None
        return max(float(total) - float(previous or 0.0), 0.0)

    def finish(self, req: AgentRequest, exit_code: int | None, stderr_tail: str) -> AgentResult:
        result = self.result
        if result is None:
            detail = {"exit_code": exit_code}
            message = "the harness ended without a result event"
            if stderr_tail.strip():
                message += f": {stderr_tail.strip()[-300:]}"
            return AgentResult(
                "failed",
                text="".join(self.texts),
                session_id=self.session_id,
                harness_version=self.version,
                model=self.model,
                permission_denials=self.denials,
                error=ErrorInfo("no_result", message, detail),
            )
        subtype = result.get("subtype")
        denials = self.denials + [
            Denial(str(d.get("tool_name")), "denied by permissions")
            for d in result.get("permission_denials") or []
        ]
        base = AgentResult(
            "succeeded",
            text=str(result.get("result") or "".join(self.texts)),
            output=result.get("structured_output"),
            session_id=result.get("session_id") or self.session_id,
            usage=normalize_usage(result.get("usage")),
            cost_usd=self._delta(result),
            session_total_usd=result.get("total_cost_usd"),
            num_turns=result.get("num_turns"),
            duration_s=float(result.get("duration_ms") or 0) / 1000,
            harness_version=self.version,
            model=self.model,
            permission_denials=denials,
        )
        detail = {"subtype": subtype, "terminal_reason": result.get("terminal_reason")}
        errors = "; ".join(str(e) for e in result.get("errors") or []) or str(
            result.get("result") or ""
        )
        if subtype == "success" and not result.get("is_error"):
            return base
        if subtype == "error_max_budget_usd":
            base.outcome = "budget_exceeded"
            base.error = ErrorInfo("budget", "the harness reached its spend cap", detail)
        elif subtype == "error_max_structured_output_retries":
            base.outcome = "schema_invalid"
            base.error = ErrorInfo(
                "schema", errors or "structured output retries exhausted", detail
            )
        elif subtype == "error_max_turns":
            base.outcome = "failed"
            base.error = ErrorInfo("harness_error", "the harness reached max_turns", detail)
        else:
            base.outcome = "failed"
            base.error = ErrorInfo("harness_error", errors or f"harness error ({subtype})", detail)
        return base
