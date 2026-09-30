"""The `agent` node: one harness session through an adapter (spec §5.1, §8.1)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from arcflow import jsonschemas
from arcflow.adapters import (
    Adapter,
    AdapterEvent,
    AgentRequest,
    AgentResult,
    ErrorInfo,
    Log,
    PermissionDenied,
    SessionSpec,
    SessionStarted,
    Text,
    ToolCall,
    ToolResult,
    Usage,
    UsageUpdate,
)
from arcflow.engine.nodes import AttemptResult, VisitContext
from arcflow.engine.structured import extract_json, fix_message, json_instruction
from arcflow.templates import parse_template

RESUME_PROMPT = (
    "Your previous run was interrupted. Continue the task and finish with the required output."
)
MAX_TEXT = 256 * 1024  # of `text` kept in state (spec §6.3); the full text is in the visit dir
PROGRESS_INTERVAL_S = 0.2  # at most five progress events per second


class AgentExecutor:
    default_on_resume = "resume"
    handles_timeout = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    def attempt_info(self, ctx: VisitContext) -> dict[str, Any]:
        """Choose the attempt's session before `attempt_started` records it."""
        adapter = ctx.runner.adapter(ctx.config["harness"])
        session = _session(ctx, adapter.capabilities())
        ctx.scratch["session"] = session
        return {
            "adapter": adapter.name,
            "session_mode": session.mode,
            "resume_session_id": session.resume_id,
        }

    async def run(self, ctx: VisitContext) -> AttemptResult:
        runner = ctx.runner
        config = ctx.config
        adapter = runner.adapter(config["harness"])
        caps = adapter.capabilities()
        session: SessionSpec = ctx.scratch["session"]
        schema = runner.flow.schema_for(ctx.node, "output_schema")
        native = caps.structured_output
        prompt = self._text(ctx, "prompt") or ""
        if (
            ctx.resume is not None
            and session.mode == "resume"
            and ctx.resume.get("mode") == "resume"
        ):
            prompt = RESUME_PROMPT
        if schema is not None and not native:
            prompt += json_instruction(schema)
        instructions = self._text(ctx, "instructions")
        visit_dir = runner.run_dir.visit_dir(ctx.node.id, ctx.visit)
        (visit_dir / "prompt.md").write_text(prompt)
        if instructions is not None:
            (visit_dir / "instructions.md").write_text(instructions)
        assert ctx.attempt_dir is not None
        request = AgentRequest(
            run_id=runner.meta["id"],
            node_id=ctx.node.id,
            visit=ctx.visit,
            attempt=ctx.attempt,
            prompt=prompt,
            instructions=instructions,
            cwd=str(runner.node_workdir(ctx)),
            add_dirs=[str(d) for d in config.get("add_dirs") or []],
            model=config.get("model"),
            effort=config.get("effort"),
            permissions=config.get("permissions", "edit"),
            allow_tools=list(config.get("allow_tools") or []),
            deny_tools=list(config.get("deny_tools") or []),
            output_schema=schema if native else None,
            session=session,
            max_turns=config.get("max_turns"),
            budget_usd=None,
            budget_tokens=None,
            bare=bool(config.get("bare", False)),
            env=runner.process_env(ctx, adapter_vars=adapter.auth_env),
            options=dict(config.get("harness_options") or {}),
            attempt_dir=str(ctx.attempt_dir),
        )
        fixes_left = int(config.get("schema_retries", 2))
        usage = Usage()
        cost: float | None = 0.0
        schema_errors: list[str] = []
        while True:
            request.budget_usd, request.budget_tokens = runner.allowance(ctx)
            result = await self._drive(ctx, adapter, request)
            spent = runner.record_spend(ctx, adapter.name, request.model, result)
            usage = _add_usage(usage, result.usage)
            cost = None if cost is None or spent is None else cost + spent
            if result.outcome != "succeeded" or schema is None:
                break
            output = result.output if native else extract_json(result.text)
            validator = jsonschemas.compile_schema(schema)
            problem = jsonschemas.validation_error(validator, output)
            if problem is None:
                result.output = output
                break
            schema_errors = [problem]
            if fixes_left <= 0:
                result.outcome = "schema_invalid"
                result.error = ErrorInfo(
                    "schema", f"output does not match output_schema: {problem}"
                )
                break
            fixes_left -= 1
            runner.emit(
                "schema_retry",
                {"errors": schema_errors},
                node=ctx.node.id,
                visit=ctx.visit,
                attempt=ctx.attempt,
            )
            if caps.resume and result.session_id:
                request.session = SessionSpec(
                    "resume", result.session_id, None, result.session_total_usd
                )
                request.prompt = fix_message(schema_errors)
            else:
                request.session = SessionSpec("new", None, _new_id(caps))
                request.prompt = prompt + "\n\n" + fix_message(schema_errors)
        if schema is None:
            result.output = None
        _redact_stream(ctx)
        result.usage = usage
        result.cost_usd = cost
        attempt = self._attempt_result(ctx, adapter.name, result)
        attempt.fields["schema_errors"] = (
            schema_errors if result.outcome == "schema_invalid" else []
        )
        return attempt

    def _text(self, ctx: VisitContext, key: str) -> str | None:
        """The rendered `prompt`/`instructions`, or the rendered contents of the `_file`."""
        if key in ctx.config:
            return str(ctx.config[key])
        path_value = ctx.config.get(f"{key}_file")
        if path_value is None:
            return None
        path = ctx.node.where(f"{key}_file").base_dir / str(path_value)
        rendered = parse_template(path.read_text(encoding="utf-8")).render(
            ctx.namespace, ctx.runner.clock.now
        )
        return str(rendered)

    async def _drive(
        self, ctx: VisitContext, adapter: Adapter, request: AgentRequest
    ) -> AgentResult:
        runner = ctx.runner
        forward = _Forwarder(ctx)
        timeout = runner.timeout_for(ctx.node, ctx.config)
        task: asyncio.Future[AgentResult] = asyncio.ensure_future(adapter.run(request, forward))
        stopper: asyncio.Future[Any] = asyncio.ensure_future(ctx.stop.event.wait())
        waiting: set[asyncio.Future[Any]] = {task, stopper}
        try:
            done, _ = await asyncio.wait(
                waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            if stopper in done:
                request.stop_reason = (
                    "cancel"
                    if ctx.stop.reason == "cancel"
                    else ("budget" if ctx.stop.reason == "budget" else "shutdown")
                )
            else:
                request.stop_reason = "timeout"
            task.cancel()
            try:
                return await task
            except asyncio.CancelledError:
                outcome = {"timeout": "timed_out", "budget": "budget_exceeded"}.get(
                    request.stop_reason, "cancelled"
                )
                return AgentResult(
                    outcome,  # type: ignore[arg-type]
                    error=ErrorInfo("harness_error", "the adapter did not report a result"),
                )
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper

    def _attempt_result(
        self, ctx: VisitContext, harness: str, result: AgentResult
    ) -> AttemptResult:
        visit_dir = ctx.runner.run_dir.visit_dir(ctx.node.id, ctx.visit)
        text = result.text or ""
        if len(text) > MAX_TEXT:
            (visit_dir / "text.md").write_text(text)
            text = text[:MAX_TEXT]
        fields = {
            "output": result.output,
            "text": text,
            "session_id": result.session_id,
            "harness": harness,
            "harness_version": result.harness_version,
            "model": result.model,
            "usage": result.usage.to_json(),
            "cost_usd": result.cost_usd,
            "cost_estimated": True,
            "num_turns": result.num_turns,
            "permission_denials": [
                {"tool": d.tool, "reason": d.reason} for d in result.permission_denials
            ],
            "schema_errors": [],
        }
        outcome: str = result.outcome
        if ctx.stop.reason == "shutdown":
            outcome = "interrupted"
        error = result.error.to_json() if result.error else None
        if outcome == "timed_out" and error is None:
            error = {"kind": "timeout", "message": "the agent timed out"}
        extra = {
            "usage": result.usage.to_json(),
            "cost_usd": result.cost_usd,
            "stopped_by": result.stopped_by,
            "session_id": result.session_id,
            "session_total_usd": result.session_total_usd,
        }
        return AttemptResult(outcome, fields, error, extra=extra)


def _new_id(capabilities: Any) -> str | None:
    return str(uuid.uuid4()) if capabilities.session_id == "caller" else None


def _add_usage(total: Usage, more: Usage) -> Usage:
    return Usage(
        total.input_tokens + more.input_tokens,
        total.cached_input_tokens + more.cached_input_tokens,
        total.output_tokens + more.output_tokens,
        total.reasoning_tokens + more.reasoning_tokens,
    )


def _session(ctx: VisitContext, capabilities: Any) -> SessionSpec:
    """The session for this attempt (spec §5.1.3, §6.6, §7.5)."""
    state = ctx.runner.state
    setting = ctx.config.get("session", "new")
    new_id = str(uuid.uuid4()) if capabilities.session_id == "caller" else None
    previous_attempt = _previous_attempt_session(state, ctx)

    def resume(session_id: str | None) -> SessionSpec:
        if session_id and capabilities.resume:
            return SessionSpec("resume", session_id, None, _session_total(state, session_id))
        return SessionSpec("new", None, new_id)

    if ctx.resume is not None and ctx.resume.get("mode") == "resume":
        return resume(ctx.resume.get("session_id"))
    if ctx.attempt > 1 and setting != "new" and previous_attempt:
        return resume(previous_attempt)
    if setting == "new":
        return SessionSpec("new", None, new_id)
    if setting == "continue":
        own = state["nodes"].get(ctx.node.id) or {}
        return resume(own.get("session_id")) if ctx.visit > 1 else SessionSpec("new", None, new_id)
    source_key = "resume" if "resume" in setting else "fork"
    source = state["nodes"].get(setting[source_key]) or {}
    session_id = source.get("session_id")
    if session_id is None:
        ctx.runner.emit(
            "warning",
            {
                "code": "W-SESSION-NEW",
                "message": f"{setting[source_key]} has no session yet; starting a new one",
            },
            node=ctx.node.id,
            visit=ctx.visit,
        )
        return SessionSpec("new", None, new_id)
    if source_key == "fork":
        if capabilities.fork:
            return SessionSpec("fork", session_id, new_id, _session_total(state, session_id))
        ctx.runner.emit(
            "warning",
            {"code": "W-FORK-AS-RESUME", "message": "the adapter cannot fork; resuming instead"},
            node=ctx.node.id,
            visit=ctx.visit,
        )
    return resume(session_id)


def _session_total(state: dict[str, Any], session_id: str) -> float | None:
    total = state.get("sessions", {}).get(session_id)
    return None if total is None else float(total)


def _previous_attempt_session(state: dict[str, Any], ctx: VisitContext) -> str | None:
    progress = state.get("in_progress") or {}
    for attempt in reversed(progress.get("attempts") or []):
        if attempt.get("attempt", 0) < ctx.attempt and attempt.get("session_id"):
            return str(attempt["session_id"])
    return None


class _Forwarder:
    """Turns adapter events into run events (spec §8.1, Appendix B)."""

    def __init__(self, ctx: VisitContext) -> None:
        self.ctx = ctx
        self.last_progress = float("-inf")

    def __call__(self, event: AdapterEvent) -> None:
        ctx = self.ctx
        where: dict[str, Any] = {"node": ctx.node.id, "visit": ctx.visit, "attempt": ctx.attempt}
        if isinstance(event, SessionStarted):
            ctx.runner.emit("session_started", {"session_id": event.session_id}, **where)
            return
        if isinstance(event, PermissionDenied):
            ctx.runner.emit(
                "permission_denied", {"tool": event.tool, "reason": event.reason}, **where
            )
            return
        if isinstance(event, UsageUpdate):
            ctx.runner.on_usage(ctx, event)
        now = ctx.runner.clock.monotonic()
        if now - self.last_progress < PROGRESS_INTERVAL_S:
            return
        self.last_progress = now
        ctx.runner.emit("progress", _progress(event), **where)


def _progress(event: AdapterEvent) -> dict[str, Any]:
    if isinstance(event, Text):
        return {"kind": "text", "summary": event.delta[-200:]}
    if isinstance(event, ToolCall):
        return {"kind": "tool_call", "summary": f"{event.name}: {event.summary}"[:200]}
    if isinstance(event, ToolResult):
        mark = "ok" if event.ok else "failed"
        return {"kind": "tool_result", "summary": f"{event.name} {mark}: {event.summary}"[:200]}
    if isinstance(event, UsageUpdate):
        return {"kind": "usage", "summary": f"{event.usage.total_tokens} tokens"}
    if isinstance(event, Log):
        return {"kind": "log", "summary": f"{event.level}: {event.message}"[:200]}
    return {"kind": "log", "summary": repr(event)[:200]}


def _redact_stream(ctx: VisitContext) -> None:
    """Apply `redact` patterns to the raw harness stream unless redact_streams is
    off (spec §12.4)."""
    runner = ctx.runner
    if not runner.redactor or not runner.config["redact_streams"] or ctx.attempt_dir is None:
        return
    stream = ctx.attempt_dir / "stream.jsonl"
    if stream.exists():
        stream.write_text(
            runner.redactor.text(stream.read_text(encoding="utf-8", errors="replace"))
        )
