"""Tier 2: command adapters declared in `.whisperwind/harnesses/<name>.yaml`.

A command adapter drives any CLI with JSON-lines output, without code: the file
gives the command line, how to send the prompt, how to resume, and expressions
(the expression language, with `event` bound to each parsed line) that pick the session ID,
text, usage, result and errors out of the stream. Placeholders are substituted
per argv element, never through a shell.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    Usage,
    UsageUpdate,
)
from whisperwind.adapters.process import ProcessAdapter, StreamParser
from whisperwind.expr import EvalError, ExprError, Expression, parse
from whisperwind.flowspec import duration
from whisperwind.problems import Problem, join_pointer
from whisperwind.schema import Bool, Field, ListOf, MapOf, Str, Struct
from whisperwind.yamlio import Document, load_file

PLACEHOLDERS = ("prompt", "prompt_file", "schema_file", "session_id", "cwd", "model", "attempt_dir")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
PROFILES = ("read-only", "edit", "full")

MAPPING = Struct({"when": Field(Str(), required=True), "value": Field(Str(), required=True)})
HARNESS_FILE = Struct(
    {
        "name": Field(Str(pattern=r"^[a-z][a-z0-9_-]*$"), required=True),
        "protocol": Field(Str(enum=("command", "aap"))),
        "command": Field(ListOf(Str(), min_items=1), required=True),
        "prompt_via": Field(Str(enum=("argv", "stdin", "file"))),
        "resume_command": Field(ListOf(Str(), min_items=1)),
        "interactive_command": Field(ListOf(Str(), min_items=1)),
        "capabilities": Field(
            Struct(
                {
                    "structured_output": Field(Bool()),
                    "resume": Field(Bool()),
                    "tokens": Field(Bool()),
                    "cost_usd": Field(Bool()),
                }
            )
        ),
        "stream": Field(
            Struct(
                {
                    "format": Field(Str(enum=("jsonl",))),
                    "session_id": Field(MAPPING),
                    "text": Field(MAPPING),
                    "usage": Field(
                        Struct(
                            {
                                "when": Field(Str(), required=True),
                                "input_tokens": Field(Str()),
                                "cached_input_tokens": Field(Str()),
                                "output_tokens": Field(Str()),
                                "cost_usd": Field(Str()),
                            }
                        )
                    ),
                    "result": Field(
                        Struct(
                            {
                                "when": Field(Str(), required=True),
                                "success": Field(Str(), required=True),
                                "text": Field(Str()),
                                "output": Field(Str()),
                            }
                        )
                    ),
                    "error": Field(
                        Struct(
                            {
                                "when": Field(Str(), required=True),
                                "message": Field(Str(), required=True),
                            }
                        )
                    ),
                }
            ),
            required=True,
        ),
        "permissions": Field(MapOf(ListOf(Str()))),
        "env": Field(ListOf(Str())),
        "tested_versions": Field(Str()),
        "version_command": Field(ListOf(Str(), min_items=1)),
        "grace": Field(duration()),
    },
    doc="An Whisperwind command adapter (.whisperwind/harnesses/<name>.yaml).",
)

# Stream keys whose sub-keys are expressions.
_EXPRESSION_KEYS = {
    "session_id": ("when", "value"),
    "text": ("when", "value"),
    "usage": ("when", "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd"),
    "result": ("when", "success", "text", "output"),
    "error": ("when", "message"),
}


@dataclass
class HarnessSpec:
    path: Path
    data: dict[str, Any]
    expressions: dict[tuple[str, str], Expression]

    @property
    def name(self) -> str:
        return str(self.data["name"])


def harness_dir(root: Path) -> Path:
    return root / ".whisperwind" / "harnesses"


def find(root: Path, name: str) -> Path | None:
    for suffix in (".yaml", ".yml"):
        path = harness_dir(root) / f"{name}{suffix}"
        if path.is_file():
            return path
    return None


def list_names(root: Path) -> list[str]:
    directory = harness_dir(root)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.iterdir() if p.suffix in (".yaml", ".yml"))


def load_spec(path: Path) -> tuple[HarnessSpec | None, list[Problem]]:
    """Load and check a harness file; the spec is returned only without errors."""
    doc, problems = load_file(path)
    if doc is None:
        return None, problems
    data = doc.data if isinstance(doc.data, dict) else {}
    if not isinstance(doc.data, dict):
        return None, [doc.problem("E-SCHEMA", "a harness file must be a mapping", "")]
    for code, message, pointer, is_key in HARNESS_FILE.check(data, ""):
        problems.append(doc.problem(code, message, pointer, key=is_key))
    if problems:
        return None, problems
    if data["name"] != path.stem:
        problems.append(
            doc.problem("E-SCHEMA", f"name must match the file name ({path.stem})", "/name")
        )
    for key in ("command", "resume_command", "interactive_command"):
        for index, element in enumerate(data.get(key) or []):
            for name in _PLACEHOLDER.findall(element):
                if name not in PLACEHOLDERS:
                    problems.append(
                        doc.problem(
                            "E-SCHEMA",
                            f"unknown placeholder {{{name}}}; use one of {', '.join(PLACEHOLDERS)}",
                            join_pointer(f"/{key}", index),
                        )
                    )
    for profile in data.get("permissions") or {}:
        if profile not in PROFILES:
            problems.append(
                doc.problem(
                    "E-SCHEMA",
                    f"unknown permission profile {profile!r}",
                    f"/permissions/{profile}",
                    key=True,
                )
            )
    expressions = _parse_expressions(doc, data, problems)
    if any(p.is_error for p in problems):
        return None, problems
    return HarnessSpec(path, data, expressions), problems


def _parse_expressions(
    doc: Document, data: dict[str, Any], problems: list[Problem]
) -> dict[tuple[str, str], Expression]:
    parsed: dict[tuple[str, str], Expression] = {}
    for section, keys in _EXPRESSION_KEYS.items():
        mapping = data["stream"].get(section)
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            if key not in mapping:
                continue
            pointer = f"/stream/{section}/{key}"
            try:
                expression = parse(str(mapping[key]))
            except ExprError as exc:
                problems.append(doc.problem(exc.code, exc.message, pointer))
                continue
            unknown = {r.path[0] for r in expression.references()} - {"event"}
            if unknown:
                problems.append(
                    doc.problem(
                        "E-UNKNOWN-REF",
                        f"only `event` is available here, not {sorted(unknown)[0]!r}",
                        pointer,
                    )
                )
                continue
            parsed[(section, key)] = expression
    return parsed


def substitute(template: list[str], values: dict[str, str | None]) -> list[str]:
    """Replace placeholders per argv element. An element whose placeholder is unset
    is dropped, together with the flag element right before it (`["-m", "{model}"]`)."""
    argv: list[str] = []
    for element in template:
        names = _PLACEHOLDER.findall(element)
        if any(not values.get(name) for name in names):
            if argv and argv[-1].startswith("-") and element.startswith("{"):
                argv.pop()
            continue
        argv.append(_PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), element))
    return argv


class CommandAdapter(ProcessAdapter):
    name: ClassVar[str] = "command"

    def __init__(self, spec: HarnessSpec) -> None:
        super().__init__()
        self.spec = spec
        self.name = spec.name  # type: ignore[misc]
        self.binary = spec.data["command"][0]  # type: ignore[misc]
        self.auth_env = tuple(spec.data.get("env") or ())  # type: ignore[misc]
        settings: dict[str, Any] = {}
        for key in ("tested_versions", "grace"):
            if key in spec.data:
                settings[key] = spec.data[key]
        self.configure(settings)

    def configure(self, settings: dict[str, Any]) -> None:
        merged = {k: v for k, v in self.spec.data.items() if k in ("tested_versions", "grace")}
        merged.update(settings)
        super().configure(merged)

    def capabilities(self) -> Capabilities:
        declared = self.spec.data.get("capabilities") or {}
        stream = self.spec.data["stream"]
        return Capabilities(
            structured_output=bool(declared.get("structured_output", False)),
            session_id="harness" if "session_id" in stream else "none",
            resume=bool(declared.get("resume", False)) and "resume_command" in self.spec.data,
            fork=False,
            interactive="interactive_command" in self.spec.data,
            cost_usd=bool(declared.get("cost_usd", False)),
            tokens=bool(declared.get("tokens", False)),
            permission_profiles=frozenset(self.spec.data.get("permissions") or {}),
            streaming=True,
        )

    def version_command(self) -> list[str]:
        return list(self.spec.data.get("version_command") or [self.command_path, "--version"])

    def command(self, req: AgentRequest) -> list[str]:
        attempt_dir = Path(req.attempt_dir)
        data = self.spec.data
        prompt_via = data.get("prompt_via", "stdin")
        resuming = (
            req.session.mode != "new" and bool(req.session.resume_id) and "resume_command" in data
        )
        template = list(data["resume_command"] if resuming else data["command"])
        template[0] = self.command_path if template[0] == data["command"][0] else template[0]
        values: dict[str, str | None] = {
            "prompt": req.prompt if prompt_via == "argv" else None,
            "prompt_file": None,
            "schema_file": None,
            "session_id": req.session.resume_id if resuming else None,
            "cwd": req.cwd,
            "model": req.model,
            "attempt_dir": req.attempt_dir,
        }
        if prompt_via == "file":
            prompt_file = attempt_dir / "prompt.txt"
            prompt_file.write_text(req.prompt)
            values["prompt_file"] = str(prompt_file)
        if req.output_schema is not None:
            schema_file = attempt_dir / "output-schema.json"
            schema_file.write_text(json.dumps(req.output_schema))
            values["schema_file"] = str(schema_file)
        argv = substitute(template, values)
        argv += list((data.get("permissions") or {}).get(req.permissions) or [])
        return argv

    def stdin(self, req: AgentRequest) -> str | None:
        return req.prompt if self.spec.data.get("prompt_via", "stdin") == "stdin" else None

    def parser(self, req: AgentRequest) -> StreamParser:
        return CommandStream(self.spec, req)

    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None:
        template = self.spec.data.get("interactive_command")
        if not template:
            return None
        return substitute(list(template), {"session_id": session_id, "cwd": cwd})


class CommandStream(StreamParser):
    def __init__(self, spec: HarnessSpec, req: AgentRequest) -> None:
        self.spec = spec
        self.req = req
        self.session_id: str | None = req.session.resume_id if req.session.mode != "new" else None
        self.texts: list[str] = []
        self.usage = Usage()
        self.cost: float | None = None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def _eval(self, section: str, key: str, event: dict[str, Any]) -> Any:
        expression = self.spec.expressions.get((section, key))
        if expression is None:
            return None
        return expression.evaluate({"event": event})

    def _matches(self, section: str, event: dict[str, Any]) -> bool:
        return (section, "when") in self.spec.expressions and self._eval(
            section, "when", event
        ) is True

    def feed(self, event: dict[str, Any]) -> list[AdapterEvent]:
        out: list[AdapterEvent] = []
        try:
            if self._matches("session_id", event):
                value = self._eval("session_id", "value", event)
                if value and value != self.session_id:
                    self.session_id = str(value)
                    out.append(SessionStarted(self.session_id))
            if self._matches("text", event):
                value = self._eval("text", "value", event)
                if value:
                    self.texts.append(str(value))
                    out.append(Text(str(value)))
            if self._matches("usage", event):
                self.usage = Usage(
                    input_tokens=int(self._eval("usage", "input_tokens", event) or 0),
                    cached_input_tokens=int(self._eval("usage", "cached_input_tokens", event) or 0),
                    output_tokens=int(self._eval("usage", "output_tokens", event) or 0),
                )
                cost = self._eval("usage", "cost_usd", event)
                self.cost = None if cost is None else float(cost)
                out.append(UsageUpdate(self.usage, self.cost))
            if self._matches("error", event):
                self.error = str(self._eval("error", "message", event))
                out.append(Log("error", self.error))
            if self._matches("result", event):
                self.result = {
                    "success": self._eval("result", "success", event) is True,
                    "text": self._eval("result", "text", event),
                    "output": self._eval("result", "output", event),
                }
        except (EvalError, ExprError, TypeError, ValueError) as exc:
            out.append(Log("warning", f"stream mapping failed: {exc}"))
        return out

    def finish(self, req: AgentRequest, exit_code: int | None, stderr_tail: str) -> AgentResult:
        text = "".join(self.texts)
        detail = {"exit_code": exit_code}
        base = AgentResult(
            "succeeded",
            text=text,
            session_id=self.session_id,
            usage=self.usage,
            cost_usd=self.cost,
            model=req.model,
        )
        if self.result is None:
            base.outcome = "failed"
            message = self.error or "the harness ended without a result event"
            kind = "harness_error" if self.error else "no_result"
            if stderr_tail.strip() and not self.error:
                message += f": {stderr_tail.strip()[-300:]}"
            base.error = ErrorInfo(kind, message, detail)
            return base
        if self.result["text"] is not None:
            base.text = str(self.result["text"])
        base.output = self.result["output"]
        if not self.result["success"]:
            base.outcome = "failed"
            base.error = ErrorInfo(
                "harness_error", self.error or "the harness reported failure", detail
            )
        return base


def harness_json_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Karetski/whisperwind/schemas/harness-1.json",
        "title": "Whisperwind command adapter",
        **HARNESS_FILE.json_schema(),
    }
