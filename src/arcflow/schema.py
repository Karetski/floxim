"""A small type language for flow files.

One declaration drives two things that must never disagree (spec §3.1): the
validator that reports `E-SCHEMA` / `E-UNKNOWN-KEY` problems with positions,
and the JSON Schema published by `arcflow schema flow` for editors and agents.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from arcflow.problems import join_pointer

# (code, message, pointer, is_key_problem)
Finding = tuple[str, str, str, bool]

TEMPLATE_RE = re.compile(r"\$\{\{")
IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"


def is_template(value: Any) -> bool:
    return isinstance(value, str) and bool(TEMPLATE_RE.search(value))


def kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class Type:
    """Base class. `templated` means a `${{ }}` string is accepted in place of the value."""

    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        raise NotImplementedError

    def check(self, value: Any, pointer: str) -> list[Finding]:
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def _accepts_template(self, value: Any) -> bool:
        return self.templated and is_template(value)

    def _wrap_template(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.templated and schema.get("type") != "string":
            return {"anyOf": [schema, {"type": "string", "pattern": r"\$\{\{"}]}
        return schema

    def _mismatch(self, value: Any, pointer: str, expected: str) -> list[Finding]:
        return [("E-SCHEMA", f"expected {expected}, got {kind_of(value)}", pointer, False)]


@dataclass
class Str(Type):
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"string"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if not isinstance(value, str):
            return self._mismatch(value, pointer, "a string")
        if self._accepts_template(value):
            return []
        if self.enum is not None and value not in self.enum:
            allowed = ", ".join(self.enum)
            return [("E-SCHEMA", f"must be one of: {allowed}; got {value!r}", pointer, False)]
        if self.pattern is not None and not re.search(self.pattern, value):
            return [("E-SCHEMA", f"{value!r} does not match {self.pattern}", pointer, False)]
        return []

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string"}
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        elif self.pattern is not None and not self.templated:
            schema["pattern"] = self.pattern
        return _describe(schema, self.doc)


@dataclass
class Int(Type):
    minimum: int | None = None
    allow_none: bool = False  # the string "none" disables a limit
    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"integer", "string"} if self.allow_none else {"integer"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if self.allow_none and value == "none":
            return []
        if self._accepts_template(value):
            return []
        if isinstance(value, bool) or not isinstance(value, int):
            return self._mismatch(value, pointer, "an integer" + (' or "none"' * self.allow_none))
        if self.minimum is not None and value < self.minimum:
            return [("E-SCHEMA", f"must be at least {self.minimum}", pointer, False)]
        return []

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "integer"}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.allow_none:
            schema = {"anyOf": [schema, {"const": "none"}]}
        return _describe(self._wrap_template(schema), self.doc)


@dataclass
class Num(Type):
    minimum: float | None = None
    allow_none: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"integer", "number", "string"} if self.allow_none else {"integer", "number"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if self.allow_none and value == "none":
            return []
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self._mismatch(value, pointer, "a number" + (' or "none"' * self.allow_none))
        if self.minimum is not None and value < self.minimum:
            return [("E-SCHEMA", f"must be at least {self.minimum}", pointer, False)]
        return []

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "number"}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.allow_none:
            schema = {"anyOf": [schema, {"const": "none"}]}
        return _describe(schema, self.doc)


@dataclass
class Bool(Type):
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"boolean"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if not isinstance(value, bool):
            return self._mismatch(value, pointer, "true or false")
        return []

    def json_schema(self) -> dict[str, Any]:
        return _describe({"type": "boolean"}, self.doc)


@dataclass
class AnyValue(Type):
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"null", "boolean", "integer", "number", "string", "array", "object"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        return []

    def json_schema(self) -> dict[str, Any]:
        return _describe({}, self.doc)


@dataclass
class Checked(Type):
    """A string or integer checked by a function, e.g. durations and sizes."""

    name: str
    validate: Callable[[Any], str | None]  # returns an error message or None
    json: dict[str, Any]
    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"string", "integer"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if self._accepts_template(value):
            return []
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return self._mismatch(value, pointer, self.name)
        error = self.validate(value)
        return [("E-SCHEMA", error, pointer, False)] if error else []

    def json_schema(self) -> dict[str, Any]:
        schema = dict(self.json)
        if self.templated:
            schema = {"anyOf": [schema, {"type": "string", "pattern": r"\$\{\{"}]}
        return _describe(schema, self.doc)


@dataclass
class ListOf(Type):
    item: Type
    min_items: int | None = None
    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"array", "string"} if self.templated else {"array"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if self._accepts_template(value):
            return []
        if not isinstance(value, list):
            return self._mismatch(value, pointer, "a list")
        findings: list[Finding] = []
        if self.min_items is not None and len(value) < self.min_items:
            findings.append(
                ("E-SCHEMA", f"needs at least {self.min_items} item(s)", pointer, False)
            )
        for index, item in enumerate(value):
            findings += self.item.check(item, join_pointer(pointer, index))
        return findings

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "array", "items": self.item.json_schema()}
        if self.min_items is not None:
            schema["minItems"] = self.min_items
        return _describe(self._wrap_template(schema), self.doc)


@dataclass
class MapOf(Type):
    """A mapping with free-form keys. `identifier_keys` are checked later (E-BAD-ID)."""

    value: Type
    identifier_keys: bool = False
    templated: bool = False
    doc: str = ""

    def kinds(self) -> set[str]:
        return {"object", "string"} if self.templated else {"object"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if self._accepts_template(value):
            return []
        if not isinstance(value, dict):
            return self._mismatch(value, pointer, "a mapping")
        findings: list[Finding] = []
        for key, item in value.items():
            findings += self.value.check(item, join_pointer(pointer, key))
        return findings

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": self.value.json_schema(),
        }
        if self.identifier_keys:
            schema["propertyNames"] = {"pattern": IDENTIFIER}
        return _describe(self._wrap_template(schema), self.doc)


@dataclass
class Field:
    type: Type
    required: bool = False


@dataclass
class Struct(Type):
    """A mapping with known keys. Keys starting with `x-` are always allowed."""

    fields: dict[str, Field]
    doc: str = ""
    extra_keys: Callable[[str], bool] = field(default=lambda key: key.startswith("x-"))
    # Partial nodes and templates: `null` removes an inherited value (spec §3.6).
    null_removes: bool = False

    def kinds(self) -> set[str]:
        return {"object"}

    def check(self, value: Any, pointer: str) -> list[Finding]:
        if not isinstance(value, dict):
            return self._mismatch(value, pointer, "a mapping")
        findings: list[Finding] = []
        for key, item in value.items():
            child = join_pointer(pointer, key)
            spec = self.fields.get(key)
            if spec is None:
                if not self.extra_keys(key):
                    findings.append(("E-UNKNOWN-KEY", _unknown_key(key, self.fields), child, True))
                continue
            if item is None and self.null_removes:
                continue
            findings += spec.type.check(item, child)
        for key, spec in self.fields.items():
            if spec.required and key not in value:
                findings.append(("E-SCHEMA", f"missing required key {key!r}", pointer, False))
        return findings

    def json_schema(self) -> dict[str, Any]:
        properties = {key: spec.type.json_schema() for key, spec in self.fields.items()}
        if self.null_removes:
            properties = {k: {"anyOf": [v, {"type": "null"}]} for k, v in properties.items()}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "patternProperties": {"^x-": {}},
            "additionalProperties": False,
        }
        required = [key for key, spec in self.fields.items() if spec.required]
        if required:
            schema["required"] = required
        return _describe(schema, self.doc)


@dataclass
class OneOf(Type):
    """Alternatives, chosen by the value's kind; ties are resolved by fewest findings."""

    alternatives: tuple[Type, ...]
    doc: str = ""

    def kinds(self) -> set[str]:
        return set().union(*(alt.kinds() for alt in self.alternatives))

    def check(self, value: Any, pointer: str) -> list[Finding]:
        kind = kind_of(value)
        candidates = [
            alt
            for alt in self.alternatives
            if kind in alt.kinds() or (kind == "integer" and "number" in alt.kinds())
        ]
        if not candidates:
            expected = " or ".join(sorted(self.kinds()))
            return self._mismatch(value, pointer, expected)
        results = [alt.check(value, pointer) for alt in candidates]
        return min(results, key=len)

    def json_schema(self) -> dict[str, Any]:
        return _describe({"anyOf": [alt.json_schema() for alt in self.alternatives]}, self.doc)


def _describe(schema: dict[str, Any], doc: str) -> dict[str, Any]:
    if doc:
        schema["description"] = doc
    return schema


def _unknown_key(key: str, fields: Iterable[str]) -> str:
    suggestion = closest(key, fields)
    return f"unknown key {key!r}" + (f" (did you mean {suggestion!r}?)" if suggestion else "")


def closest(word: str, options: Iterable[str]) -> str | None:
    import difflib

    matches = difflib.get_close_matches(word, list(options), n=1, cutoff=0.75)
    return matches[0] if matches else None
