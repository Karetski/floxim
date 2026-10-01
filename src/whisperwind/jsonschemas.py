"""JSON Schema checking with `fastjsonschema` (pure Python).

fastjsonschema implements JSON Schema up to draft-07, so that is the dialect
flow files use for `output_schema` and input schemas. Remote
`$ref`s are never fetched.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from functools import lru_cache
from importlib import resources
from typing import Any, cast

import fastjsonschema

DRAFT_07 = "http://json-schema.org/draft-07/schema#"
_ACCEPTED_DIALECTS = {DRAFT_07, "http://json-schema.org/draft-07/schema", None}

Validator = Callable[[Any], Any]


def _no_remote(uri: str) -> Any:
    raise fastjsonschema.JsonSchemaDefinitionException(
        f"remote $ref {uri!r} is not allowed; keep schemas self-contained"
    )


_HANDLERS = {"http": _no_remote, "https": _no_remote, "file": _no_remote}


@lru_cache(maxsize=1)
def _meta_validator() -> Validator:
    text = resources.files("whisperwind").joinpath("data/json-schema-draft-07.json").read_text()
    return cast(Validator, fastjsonschema.compile(json.loads(text), handlers=_HANDLERS))


def schema_error(schema: Any) -> str | None:
    """Why `schema` is not a usable JSON Schema, or None when it is."""
    if not isinstance(schema, (dict, bool)):
        return "a JSON Schema must be an object or a boolean"
    if isinstance(schema, dict) and schema.get("$schema") not in _ACCEPTED_DIALECTS:
        return f"unsupported $schema {schema['$schema']!r}; Whisperwind uses JSON Schema draft-07"
    try:
        # fastjsonschema fills in `default` values from the schema it validates
        # against, so validate a copy and leave the user's schema untouched.
        _meta_validator()(copy.deepcopy(schema))
    except fastjsonschema.JsonSchemaValueException as exc:
        return f"not a valid JSON Schema: {exc.message}"
    try:
        fastjsonschema.compile(schema, handlers=_HANDLERS)
    except fastjsonschema.JsonSchemaDefinitionException as exc:
        return f"not a valid JSON Schema: {exc}"
    return None


def compile_schema(schema: Any) -> Validator:
    return cast(Validator, fastjsonschema.compile(schema, handlers=_HANDLERS))


def validation_error(validator: Validator, value: Any) -> str | None:
    """The first validation error for `value`, or None when it is valid. The value
    itself is never changed (defaults are filled into a copy)."""
    try:
        validator(copy.deepcopy(value))
    except fastjsonschema.JsonSchemaValueException as exc:
        return str(exc.message)
    return None
