"""The flow file format as data (spec §3 and §5).

`FLOW` is the declaration the validator checks against and from which
`floxim schema flow` is generated.
"""

from __future__ import annotations

from typing import Any

from floxim.schema import (
    IDENTIFIER,
    AnyValue,
    Bool,
    Checked,
    Field,
    Int,
    ListOf,
    MapOf,
    Num,
    OneOf,
    Str,
    Struct,
)
from floxim.units import duration_error, size_error

FORMAT_VERSION = 1

NODE_TYPES = (
    "agent",
    "shell",
    "condition",
    "human",
    "sleep",
    "set",
    "python",
    "subflow",
    "map",
    "handoff",
    "notify",
)
RESERVED_NODE_IDS = frozenset(
    {"end", "fail", "self", "inputs", "nodes", "visits", "vars", "run", "env", "node", "item"}
)
TERMINAL_TARGETS = frozenset({"end", "fail"})
OUTCOMES = (
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "budget_exceeded",
    "schema_invalid",
    "interrupted",
)
WORKSPACE_TYPES = frozenset({"agent", "shell", "python"})
ROUTING_KEYS = ("next", "on_error")


def duration(*, templated: bool = False, allow_none: bool = False, doc: str = "") -> Checked:
    return Checked(
        name="a duration",
        validate=lambda v: duration_error(v, allow_none),
        json={
            "type": ["string", "integer"],
            "pattern": r"^(\d+[smhd])+$|^\d+$" + ("|^none$" if allow_none else ""),
        },
        templated=templated,
        doc=doc,
    )


SIZE = Checked(
    name="a size",
    validate=size_error,
    json={"type": ["string", "integer"], "pattern": r"^\d+(B|KiB|MiB|GiB)?$"},
    doc="Bytes, or a number with B, KiB, MiB or GiB.",
)

TEMPLATE_STR = Str(templated=True)
PATH_OR_SCHEMA = OneOf(
    (Str(), MapOf(AnyValue())),
    doc="A JSON Schema: a path relative to the flow file, or an inline mapping.",
)

TARGET = Str(doc="A node ID, or `end` / `fail`.")
CASE = Struct(
    {
        "when": Field(Str(doc="Expression that must evaluate to a boolean.")),
        "to": Field(TARGET, required=True),
        "reason": Field(TEMPLATE_STR),
    }
)
NEXT = OneOf((TARGET, ListOf(CASE, min_items=1)), doc="Where to go after success (§3.9).")
ON_ERROR = OneOf(
    (TARGET, ListOf(CASE, min_items=1)),
    doc="`fail`, `continue`, a node ID, or a list of cases (§6.5).",
)

RETRY = Struct(
    {
        "max_attempts": Field(Int(minimum=1)),
        "backoff": Field(duration()),
        "on": Field(ListOf(Str(enum=OUTCOMES))),
    }
)
WORKSPACE = OneOf(
    (
        Str(enum=("shared", "worktree")),
        Struct(
            {
                "worktree": Field(Str(pattern=IDENTIFIER), required=True),
                "base": Field(TEMPLATE_STR),
                "branch": Field(TEMPLATE_STR),
                "keep": Field(Bool()),
            }
        ),
    ),
    doc="`shared`, `worktree`, or a named worktree (§6.9).",
)
BUDGET = Struct(
    {
        "usd": Field(Num(minimum=0, allow_none=True)),
        "tokens": Field(Int(minimum=0, allow_none=True)),
    }
)

# Keys every node accepts (§3.8). `workspace` and `env` are further limited by type.
COMMON: dict[str, Field] = {
    "type": Field(Str(enum=NODE_TYPES)),
    "description": Field(Str()),
    "extends": Field(Str()),
    "next": Field(NEXT),
    "on_error": Field(ON_ERROR),
    "max_visits": Field(Int(minimum=1)),
    "timeout": Field(duration(allow_none=True)),
    "retry": Field(RETRY),
    "on_resume": Field(Str(enum=("resume", "restart", "ask"))),
    "workspace": Field(WORKSPACE),
    "env": Field(MapOf(TEMPLATE_STR)),
}

SESSION = OneOf(
    (
        Str(enum=("new", "continue")),
        Struct({"resume": Field(Str(), required=True)}),
        Struct({"fork": Field(Str(), required=True)}),
    ),
    doc="§5.1.3",
)
CHOICE = OneOf(
    (Str(), Struct({"value": Field(Str(), required=True), "label": Field(Str())})),
)

TYPE_FIELDS: dict[str, dict[str, Field]] = {
    "agent": {
        "harness": Field(Str()),
        "model": Field(TEMPLATE_STR),
        "effort": Field(Str(enum=("low", "medium", "high", "max"))),
        "prompt": Field(TEMPLATE_STR),
        "prompt_file": Field(Str()),
        "instructions": Field(TEMPLATE_STR),
        "instructions_file": Field(Str()),
        "output_schema": Field(PATH_OR_SCHEMA),
        "schema_retries": Field(Int(minimum=0)),
        "session": Field(SESSION),
        "permissions": Field(Str(enum=("read-only", "edit", "full"))),
        "allow_tools": Field(ListOf(Str())),
        "deny_tools": Field(ListOf(Str())),
        "add_dirs": Field(ListOf(TEMPLATE_STR)),
        "max_turns": Field(Int(minimum=1)),
        "budget": Field(BUDGET),
        "bare": Field(Bool()),
        "harness_options": Field(MapOf(AnyValue())),
    },
    "shell": {
        "run": Field(TEMPLATE_STR),
        "args": Field(ListOf(TEMPLATE_STR, min_items=1)),
        "shell": Field(Str()),
        "cwd": Field(TEMPLATE_STR),
        "stdin": Field(TEMPLATE_STR),
        "ok_codes": Field(ListOf(Int(minimum=0), min_items=1)),
        "output": Field(Str(enum=("none", "json", "text"))),
        "output_schema": Field(PATH_OR_SCHEMA),
        "max_output": Field(SIZE),
    },
    "condition": {},
    "human": {
        "message": Field(TEMPLATE_STR),
        "message_file": Field(Str()),
        "choices": Field(ListOf(CHOICE, min_items=1)),
        "input": Field(Str(enum=("none", "text"))),
        "ack": Field(Bool()),
        "default": Field(Str()),
        "show": Field(ListOf(TEMPLATE_STR)),
        "on_wait": Field(TEMPLATE_STR),
    },
    "sleep": {
        "duration": Field(duration(templated=True)),
        "until": Field(TEMPLATE_STR),
    },
    "set": {
        "vars": Field(MapOf(AnyValue(templated=True), identifier_keys=True)),
    },
    "python": {
        "call": Field(Str(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")),
        "args": Field(MapOf(AnyValue(templated=True))),
        "output_schema": Field(PATH_OR_SCHEMA),
        "interpreter": Field(Str()),
    },
    "subflow": {
        "flow": Field(Str()),
        "inputs": Field(MapOf(AnyValue(templated=True), templated=True)),
    },
    "map": {
        "items": Field(ListOf(AnyValue(templated=True), templated=True)),
        "flow": Field(Str()),
        "inputs": Field(MapOf(AnyValue(templated=True), templated=True)),
        "max_items": Field(Int(minimum=1)),
        "on_item_error": Field(Str(enum=("fail", "continue"))),
        "concurrency": Field(Int(minimum=1)),
    },
    "handoff": {
        "from": Field(Str()),
        "message": Field(TEMPLATE_STR),
    },
    "notify": {
        "message": Field(TEMPLATE_STR),
        "command": Field(Str()),
        "webhook": Field(
            Struct(
                {
                    "url": Field(TEMPLATE_STR, required=True),
                    "method": Field(Str(enum=("POST", "PUT", "PATCH", "GET"))),
                    "headers": Field(MapOf(TEMPLATE_STR)),
                    "body": Field(AnyValue(templated=True)),
                }
            )
        ),
        "required": Field(Bool()),
    },
}

# Keys that must be present on the effective node, by type (checked after templates).
REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "agent": ("harness",),
    "python": ("call",),
    "subflow": ("flow",),
    "map": ("items", "flow"),
    "handoff": ("from",),
    "notify": ("message",),
    "condition": ("next",),
}
# Groups of which exactly one must be present on the effective node.
EXACTLY_ONE: dict[str, tuple[tuple[str, ...], ...]] = {
    "agent": (("prompt", "prompt_file"),),
    "shell": (("run", "args"),),
    "human": (("message", "message_file"),),
    "sleep": (("duration", "until"),),
    "notify": (("command", "webhook"),),
}
# Groups of which at most one may be present.
AT_MOST_ONE: dict[str, tuple[tuple[str, ...], ...]] = {
    "agent": (("instructions", "instructions_file"),),
}
# Keys holding paths to files, resolved against the flow file's directory (§2.1).
FILE_KEYS: dict[str, tuple[str, ...]] = {
    "agent": ("prompt_file", "instructions_file", "output_schema"),
    "shell": ("output_schema",),
    "human": ("message_file",),
    "python": ("output_schema",),
    "subflow": ("flow",),
    "map": ("flow",),
}
SCHEMA_KEYS = frozenset({"output_schema"})


def node_fields(node_type: str) -> dict[str, Field]:
    """All keys a node of this type accepts."""
    fields = dict(COMMON)
    if node_type not in WORKSPACE_TYPES:
        del fields["workspace"], fields["env"]
    fields.update(TYPE_FIELDS[node_type])
    return fields


def node_struct(node_type: str, *, routing: bool = True) -> Struct:
    fields = node_fields(node_type)
    if not routing:
        fields = {k: v for k, v in fields.items() if k not in ROUTING_KEYS}
    return Struct(fields)


def _union_fields() -> dict[str, Field]:
    """Every key any node type accepts. A key whose type differs between node
    types (e.g. `args`: a list for `shell`, a mapping for `python`) is typed as
    any value here; the effective node is checked against its own type later."""
    union: dict[str, Field] = dict(COMMON)
    for fields in TYPE_FIELDS.values():
        for key, spec in fields.items():
            if key in union and union[key].type is not spec.type:
                union[key] = Field(AnyValue())
            else:
                union.setdefault(key, spec)
    return union


# A node or template before `extends` is resolved and its type is known.
PARTIAL_NODE = Struct(_union_fields(), null_removes=True)

INPUT_TYPES = ("string", "integer", "number", "boolean", "object", "array")
INPUT = Struct(
    {
        "type": Field(Str(enum=INPUT_TYPES), required=True),
        "required": Field(Bool()),
        "description": Field(Str()),
        "default": Field(AnyValue()),
        "enum": Field(ListOf(AnyValue(), min_items=1)),
        "schema": Field(PATH_OR_SCHEMA),
    }
)

DEFAULT_COMMON_KEYS = ("timeout", "max_visits", "retry", "on_resume", "workspace", "env")
DEFAULTS = Struct(
    {
        **{key: COMMON[key] for key in DEFAULT_COMMON_KEYS},
        **{
            node_type: Field(
                Struct(
                    {
                        k: v
                        for k, v in node_fields(node_type).items()
                        if k not in ("type", "extends", "description", *ROUTING_KEYS)
                    }
                )
            )
            for node_type in NODE_TYPES
        },
    }
)

LIMITS = Struct(
    {
        "max_steps": Field(Int(minimum=1, allow_none=True)),
        "max_duration": Field(duration(allow_none=True)),
        "budget": Field(BUDGET),
    }
)

FLOW = Struct(
    {
        "floxim": Field(Int(minimum=1), required=False),
        "name": Field(Str(pattern=r"^[a-z0-9][a-z0-9_-]*$"), required=True),
        "description": Field(Str()),
        "inputs": Field(MapOf(INPUT, identifier_keys=True)),
        "defaults": Field(DEFAULTS),
        "limits": Field(LIMITS),
        "templates": Field(MapOf(PARTIAL_NODE, identifier_keys=True)),
        "include": Field(ListOf(Str())),
        "start": Field(Str()),
        "nodes": Field(MapOf(PARTIAL_NODE, identifier_keys=True), required=True),
        "outputs": Field(MapOf(AnyValue(templated=True), identifier_keys=True)),
        "on_wait": Field(Str()),
    },
    doc="An Floxim flow file (format version 1).",
)

INCLUDE_FILE = Struct(
    {"templates": Field(MapOf(PARTIAL_NODE, identifier_keys=True), required=True)}
)


def flow_json_schema() -> dict[str, Any]:
    """The published JSON Schema for flow files (`floxim schema flow`).

    Node entries are described per type with `if`/`then` on `type`, so editors
    complete the right keys once `type` is written.
    """
    schema = FLOW.json_schema()
    per_type: list[dict[str, Any]] = []
    for node_type in NODE_TYPES:
        per_type.append(
            {
                "if": {"properties": {"type": {"const": node_type}}, "required": ["type"]},
                "then": node_struct(node_type).json_schema(),
            }
        )
    node_schema = PARTIAL_NODE.json_schema()
    node_schema["allOf"] = per_type
    schema["properties"]["nodes"]["additionalProperties"] = node_schema
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Karetski/floxim/schemas/flow-1.json",
        "title": "Floxim flow",
        **schema,
    }


# Result fields readable as `nodes.<id>.<field>` (spec §4.3 and §5).
COMMON_RESULT_FIELDS = frozenset(
    {"outcome", "visit", "attempts", "started_at", "finished_at", "duration_s", "error", "visits"}
)
RESULT_FIELDS: dict[str, frozenset[str]] = {
    "agent": frozenset(
        {
            "output",
            "text",
            "session_id",
            "harness",
            "harness_version",
            "model",
            "usage",
            "cost_usd",
            "cost_estimated",
            "num_turns",
            "permission_denials",
            "schema_errors",
            "workspace",
            "artifacts_dir",
        }
    ),
    "shell": frozenset(
        {
            "exit_code",
            "signal",
            "stdout",
            "stderr",
            "stdout_file",
            "stderr_file",
            "stdout_truncated",
            "stderr_truncated",
            "output",
            "workspace",
            "artifacts_dir",
        }
    ),
    "condition": frozenset({"branch"}),
    "human": frozenset(
        {"choice", "text", "acknowledged", "responder", "responded_at", "via", "timed_out"}
    ),
    "sleep": frozenset({"woke_at"}),
    "set": frozenset({"values"}),
    "python": frozenset({"output", "workspace", "artifacts_dir"}),
    "subflow": frozenset({"run_id", "status", "outputs", "output"}),
    "map": frozenset({"results", "succeeded", "failed"}),
    "handoff": frozenset({"session_id", "exit_code"}),
    "notify": frozenset(),
}
# Node types whose `output` can be described by an `output_schema`.
SCHEMA_OUTPUT_TYPES = frozenset({"agent", "shell", "python"})
RUN_FIELDS = frozenset({"id", "flow", "flow_file", "started_at", "workdir", "budget"})
BUDGET_FIELDS = frozenset({"usd_spent", "tokens_spent", "usd_left", "tokens_left"})
NODE_CONTEXT_FIELDS = frozenset({"id", "visit", "attempt", "artifacts_dir", "workdir", "message"})

# Built-in attempt timeouts by node type (spec §3.4); other types have none.
DEFAULT_TIMEOUTS: dict[str, str] = {"agent": "30m", "shell": "10m", "human": "7d"}
# The longest sleep, unless limits.max_duration is longer (spec §3.4).
MAX_SLEEP = "7d"
