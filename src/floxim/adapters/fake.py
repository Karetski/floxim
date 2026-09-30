"""The fake adapter: scripted responses, no processes, no cost.

Every engine test drives agent nodes through it. A script is a list of
responses. An entry with `match` answers every call whose node, visit and
attempt match; the others answer each node's calls in order. A `delay` makes a
call slow, for timeout, cancel and budget tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ruamel.yaml import YAML

from floxim.adapters import (
    AdapterEvent,
    AgentRequest,
    AgentResult,
    Capabilities,
    Denial,
    ErrorInfo,
    Probe,
    SessionStarted,
    Text,
    Usage,
    UsageUpdate,
)
from floxim.units import parse_duration

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "match": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "node": {"type": "string"},
                "visit": {"type": "integer"},
                "attempt": {"type": "integer"},
            },
        },
        "outcome": {"enum": ["succeeded", "failed", "timed_out", "budget_exceeded", "cancelled"]},
        "output": {},
        "text": {"type": "string"},
        "usage": {"type": "object"},
        "cost_usd": {"type": ["number", "null"]},
        "delay": {"type": ["string", "integer"]},
        "session_id": {"type": "string"},
        "num_turns": {"type": "integer"},
        "error": {"type": "object"},
        "permission_denials": {"type": "array"},
        "replay": {"type": "string"},
    },
}

OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "script": {"type": "string", "description": "YAML file with a `responses` list"},
        "responses": {"type": "array", "items": RESPONSE_SCHEMA},
    },
}


class FakeAdapter:
    name: ClassVar[str] = "fake"
    auth_env: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        self._next: dict[str, int] = {}  # per node: index of its next unmatched response

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
            streaming=True,
            effort=True,
            bare=True,
        )

    def options_schema(self) -> dict[str, Any]:
        return OPTIONS_SCHEMA

    async def probe(self) -> Probe:
        return Probe(found=True, version="fake")

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None:
        return ["sh", "-c", f"echo resuming fake session {session_id}"]

    def _responses(self, req: AgentRequest) -> list[dict[str, Any]]:
        options = req.options
        if "responses" in options:
            return list(options["responses"])
        if "script" in options:
            data = YAML(typ="safe", pure=True).load(Path(options["script"]).read_text())
            return list((data or {}).get("responses") or [])
        return []

    def _pick(self, req: AgentRequest) -> dict[str, Any]:
        responses = self._responses(req)
        for response in responses:
            match = response.get("match")
            if match and all(
                match.get(k) is None or match.get(k) == v
                for k, v in (("node", req.node_id), ("visit", req.visit), ("attempt", req.attempt))
            ):
                return response
        unmatched = [r for r in responses if not r.get("match")]
        index = self._next.get(req.node_id, 0)
        if index < len(unmatched):
            self._next[req.node_id] = index + 1
            return unmatched[index]
        return {"text": "ok"}

    async def run(self, req: AgentRequest, emit: Callable[[AdapterEvent], None]) -> AgentResult:
        response = self._pick(req)
        session = req.session
        if session.mode == "resume" and session.resume_id:
            session_id = session.resume_id
        else:
            session_id = response.get("session_id") or session.new_id or str(uuid.uuid4())
        emit(SessionStarted(session_id))
        usage = Usage(
            **{k: int(v) for k, v in (response.get("usage") or {}).items() if k != "total_tokens"}
        )
        cost = response.get("cost_usd")
        delay = parse_duration(response.get("delay", 0)) or 0.0
        try:
            if delay:
                # Report half the usage up front, so budget watching can see spend.
                emit(
                    UsageUpdate(
                        Usage(usage.input_tokens // 2, 0, usage.output_tokens // 2),
                        None if cost is None else cost / 2,
                    )
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            outcome = {"timeout": "timed_out", "budget": "budget_exceeded"}.get(
                req.stop_reason or "", "cancelled"
            )
            return AgentResult(
                outcome,  # type: ignore[arg-type]
                session_id=session_id,
                usage=usage,
                cost_usd=cost,
                error=ErrorInfo("harness_error", f"stopped: {req.stop_reason}"),
                stopped_by="sigint",
            )
        text = response.get("text", "")
        if text:
            emit(Text(text))
        emit(UsageUpdate(usage, cost))
        error = response.get("error")
        return AgentResult(
            response.get("outcome", "succeeded"),
            text=text,
            output=response.get("output"),
            session_id=session_id,
            usage=usage,
            cost_usd=cost,
            num_turns=response.get("num_turns", 1),
            harness_version="fake",
            model=req.model,
            permission_denials=[Denial(**d) for d in response.get("permission_denials", [])],
            error=ErrorInfo(error.get("kind", "harness_error"), error.get("message", ""))
            if error
            else None,
        )
