"""Run inputs: parsing `--input k=v`, coercion to declared types, defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from whisperwind import jsonschemas
from whisperwind.flow import Flow


class InputError(Exception):
    """An input problem; the run is not created (exit code 3)."""


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_assignments(pairs: list[str], base_dir: Path) -> dict[str, Any]:
    """`name=value` strings from the command line; `name=@file` reads the file."""
    values: dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name:
            raise InputError(f"--input needs name=value, got {pair!r}")
        if raw.startswith("@"):
            path = base_dir / raw[1:]
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise InputError(f"input {name}: cannot read {path}: {exc}") from None
        values[name] = raw
    return values


def load_inputs_file(path: Path) -> dict[str, Any]:
    try:
        data = _yaml_load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise InputError(f"cannot read inputs file {path}: {exc}") from None
    if not isinstance(data, dict):
        raise InputError(f"inputs file {path} must contain a mapping")
    return data


def _yaml_load(text: str) -> Any:
    return json.loads(json.dumps(YAML(typ="safe", pure=True).load(text), default=str))


def resolve_inputs(flow: Flow, given: dict[str, Any], *, from_text: set[str]) -> dict[str, Any]:
    """Coerce and check inputs. Names in `from_text` came from the command line as
    strings and are coerced to the declared type; others must already have it."""
    unknown = sorted(set(given) - set(flow.inputs))
    if unknown:
        raise InputError(f"undeclared input(s): {', '.join(unknown)}")
    resolved: dict[str, Any] = {}
    for name, spec in flow.inputs.items():
        if name in given:
            value = given[name]
            if name in from_text:
                value = _coerce(name, spec["type"], value)
            _check_type(name, spec["type"], value)
        elif "default" in spec:
            value = spec["default"]
        elif spec.get("required"):
            raise InputError(f"missing required input {name!r}")
        else:
            value = None
        if value is not None:
            if "enum" in spec and value not in spec["enum"]:
                allowed = ", ".join(map(str, spec["enum"]))
                raise InputError(f"input {name!r} must be one of: {allowed}; got {value!r}")
            schema = _schema(flow, spec)
            if schema is not None:
                error = jsonschemas.validation_error(jsonschemas.compile_schema(schema), value)
                if error:
                    raise InputError(f"input {name!r} does not match its schema: {error}")
        resolved[name] = value
    return resolved


def _schema(flow: Flow, spec: dict[str, Any]) -> Any:
    schema = spec.get("schema")
    if isinstance(schema, str):
        return json.loads((flow.base_dir / schema).read_text(encoding="utf-8"))
    return schema


def _coerce(name: str, kind: str, raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    try:
        if kind == "string":
            return raw
        if kind == "integer":
            return int(text)
        if kind == "number":
            number = float(text)
            return int(number) if number.is_integer() and "." not in text else number
        if kind == "boolean":
            if text.lower() in _TRUE:
                return True
            if text.lower() in _FALSE:
                return False
            raise ValueError(text)
        return _yaml_load(raw)  # object or array: JSON or YAML
    except (ValueError, YAMLError):
        raise InputError(f"input {name!r}: cannot read {raw!r} as {kind}") from None


def _check_type(name: str, kind: str, value: Any) -> None:
    ok = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }[kind]
    if not ok:
        raise InputError(f"input {name!r} must be {kind}, got {type(value).__name__}")
