"""JSON Schemas of every command's `--json` output (spec §9.1, `arcflow schema cli`).

They are part of Arcflow's public interface: scripts and the TUI rely on them,
and the test suite validates real command output against them.
"""

from __future__ import annotations

from typing import Any

STR = {"type": "string"}
NSTR = {"type": ["string", "null"]}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
ANY: dict[str, Any] = {}


def obj(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "required": sorted(required),
        "properties": {**required, **(optional or {})},
    }


def array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


PROBLEM = obj(
    {
        "code": STR,
        "severity": {"enum": ["error", "warning", "info"]},
        "message": STR,
        "file": NSTR,
        "line": {"type": ["integer", "null"]},
        "column": {"type": ["integer", "null"]},
        "pointer": STR,
        "hint": NSTR,
    }
)
TOTALS = obj({"usd_spent": NUM, "tokens_spent": INT, "steps": INT, "active_s": NUM})
RUN_SUMMARY = obj(
    {
        "run_id": STR,
        "flow": NSTR,
        "status": STR,
        "current": NSTR,
        "pending": array(STR),
        "created_at": NSTR,
        "started_at": NSTR,
        "finished_at": NSTR,
        "parent": NSTR,
        "totals": TOTALS,
        "failure": {"type": ["object", "null"]},
    }
)
RUN_RESULT = obj(
    {"run_id": STR, "status": STR, "outputs": ANY, "failure": ANY, "totals": {"type": "object"}}
)
VALIDATE = obj(
    {
        "files": array(obj({"file": STR, "valid": BOOL, "name": NSTR})),
        "problems": array(PROBLEM),
    }
)
CHECKS = array(obj({"check": STR, "status": {"enum": ["ok", "warn", "error"]}, "detail": STR}))

# The `data` of each command's successful `--json` document.
DATA: dict[str, dict[str, Any]] = {
    "validate": VALIDATE,
    "schema": {"type": "object"},
    "run": RUN_RESULT,
    "run --detach": obj({"run_id": STR, "status": {"const": "started"}, "pid": INT}),
    "resume": RUN_RESULT,
    "resume --due": obj({"resumed": array(STR)}),
    "respond": obj(
        {
            "run_id": STR,
            "node": STR,
            "delivered": {"enum": ["inbox", "recorded"]},
            "continued": BOOL,
        }
    ),
    "cancel": obj(
        {"run_id": STR, "status": STR, "delivered": {"enum": ["requested", "cancelled"]}}
    ),
    "status": obj({"runs": array(RUN_SUMMARY)}),
    "status <run>": {
        "allOf": [
            RUN_SUMMARY,
            obj(
                {
                    "inputs": ANY,
                    "workdir": NSTR,
                    "visits": {"type": "object"},
                    "nodes": {"type": "object"},
                    "in_progress": {"type": ["object", "null"]},
                    "pending_human": {"type": "object"},
                    "outputs": ANY,
                    "vars": {"type": ["object", "null"]},
                }
            ),
        ]
    },
    "list": obj({"runs": array(RUN_SUMMARY)}),
    "wait": RUN_SUMMARY,
    "artifacts": obj(
        {"artifacts": array(obj({"node": STR, "visit": INT, "path": STR, "size": INT}))}
    ),
    "flows": obj(
        {
            "flows": array(
                obj(
                    {
                        "file": STR,
                        "name": NSTR,
                        "description": NSTR,
                        "valid": BOOL,
                        "errors": INT,
                        "warnings": INT,
                    }
                )
            )
        }
    ),
    "graph": obj(
        {
            "name": STR,
            "start": STR,
            "nodes": array(obj({"id": STR, "type": STR, "description": NSTR})),
            "edges": array(
                obj(
                    {
                        "from": STR,
                        "to": STR,
                        "via": {"enum": ["next", "on_error"]},
                        "case_index": {"type": ["integer", "null"]},
                        "when": NSTR,
                        "explicit": BOOL,
                    }
                )
            ),
        }
    ),
    "init": obj({"root": STR, "created": array(STR), "kept": array(STR)}),
    "adapters": obj(
        {
            "adapters": array(
                obj(
                    {"name": STR, "source": STR, "loadable": BOOL},
                    {
                        "capabilities": {"type": "object"},
                        "probe": {"type": "object"},
                        "problem": STR,
                    },
                )
            )
        }
    ),
    "gc": obj({"removed": array(STR), "worktrees": array(STR), "dry_run": BOOL}),
    "doctor": obj({"checks": CHECKS}),
    "adapter test": obj(
        {
            "adapter": STR,
            "cases": array(obj({"case": STR, "passed": BOOL, "problems": array(STR)})),
        }
    ),
}

ERROR = obj({"code": {"type": "string", "pattern": "^E-"}, "message": STR, "details": ANY})


def envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["ok", "data"],
                "additionalProperties": False,
                "properties": {"ok": {"const": True}, "data": data},
            },
            {
                "type": "object",
                "required": ["ok", "error"],
                "additionalProperties": False,
                "properties": {"ok": {"const": False}, "error": ERROR},
            },
        ]
    }


def cli_json_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Karetski/arcflow/schemas/cli-1.json",
        "title": "Arcflow command output (--json)",
        "definitions": {command: envelope(data) for command, data in DATA.items()},
    }
