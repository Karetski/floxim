"""The harness adapter contract (spec §8.1, ADR 0011).

The engine depends on nothing but these types. An adapter drives one harness:
it declares its capabilities, runs one request while emitting normalized
events, and returns a normalized result. Cancellation is asyncio task
cancellation: the adapter stops the harness and *returns* a result whose outcome
follows `request.stop_reason`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

Outcome = Literal[
    "succeeded", "failed", "cancelled", "timed_out", "budget_exceeded", "schema_invalid"
]
StopReason = Literal["timeout", "cancel", "shutdown", "budget"]


@dataclass(frozen=True)
class Capabilities:
    structured_output: bool = False
    session_id: Literal["caller", "harness", "none"] = "none"
    resume: bool = False
    fork: bool = False
    interactive: bool = False
    cost_usd: bool = False
    tokens: bool = False
    budget_cap: bool = False
    turn_cap: bool = False
    permission_profiles: frozenset[str] = frozenset({"read-only", "edit", "full"})
    tool_rules: bool = False
    permission_hook: bool = False
    streaming: bool = False
    effort: bool = False
    bare: bool = False

    def to_json(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["permission_profiles"] = sorted(self.permission_profiles)
        return data


@dataclass(frozen=True)
class SessionSpec:
    mode: Literal["new", "resume", "fork"] = "new"
    resume_id: str | None = None  # the session to resume or fork
    new_id: str | None = None  # a caller-chosen ID, for adapters with session_id="caller"
    # What the harness last reported as the resumed session's running total, so an
    # adapter whose harness reports session totals can report this call's delta.
    previous_cost_usd: float | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_json(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class Denial:
    tool: str
    reason: str


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)  # harness subtype, exit code, signal

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class AgentRequest:
    run_id: str
    node_id: str
    visit: int
    attempt: int
    prompt: str
    instructions: str | None
    cwd: str
    add_dirs: list[str]
    model: str | None
    effort: str | None
    permissions: str
    allow_tools: list[str]
    deny_tools: list[str]
    output_schema: dict[str, Any] | None
    session: SessionSpec
    max_turns: int | None
    budget_usd: float | None
    budget_tokens: int | None
    bare: bool
    env: dict[str, str]
    options: dict[str, Any]
    attempt_dir: str
    stop_reason: StopReason | None = None


# -- events -------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionStarted:
    session_id: str


@dataclass(frozen=True)
class Text:
    delta: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    summary: str


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class UsageUpdate:
    """Cumulative usage of the current attempt so far."""

    usage: Usage
    cost_usd: float | None = None


@dataclass(frozen=True)
class PermissionDenied:
    tool: str
    reason: str


@dataclass(frozen=True)
class Log:
    level: str
    message: str


AdapterEvent = SessionStarted | Text | ToolCall | ToolResult | UsageUpdate | PermissionDenied | Log


@dataclass
class AgentResult:
    outcome: Outcome
    text: str = ""
    output: Any = None
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_s: float = 0.0
    harness_version: str | None = None
    model: str | None = None
    permission_denials: list[Denial] = field(default_factory=list)
    error: ErrorInfo | None = None
    stopped_by: Literal["sigint", "sigterm", "sigkill"] | None = None
    # The session's running total as the harness reported it, when it reports one.
    session_total_usd: float | None = None


@dataclass(frozen=True)
class Probe:
    found: bool
    version: str | None = None
    in_tested_range: bool | None = None
    auth_hint: str | None = None
    source: str = "built-in"


class Adapter(Protocol):
    name: ClassVar[str]
    # Environment variables the harness needs for authentication (spec §12.3).
    auth_env: ClassVar[tuple[str, ...]]

    def capabilities(self) -> Capabilities: ...

    def options_schema(self) -> dict[str, Any]: ...

    async def probe(self) -> Probe: ...

    async def run(self, req: AgentRequest, emit: Callable[[AdapterEvent], None]) -> AgentResult: ...

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None: ...
