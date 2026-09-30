"""Structured agent output: prompt-based JSON and extraction."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


def json_instruction(schema: dict[str, Any]) -> str:
    """Appended to the prompt for adapters without native structured output."""
    return (
        "\n\nWhen you finish, reply with only a JSON object that matches this JSON Schema, "
        "and nothing else:\n```json\n" + json.dumps(schema, indent=2) + "\n```\n"
    )


def fix_message(errors: list[str]) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        "Your final output did not match the required JSON Schema:\n"
        f"{listed}\n"
        "Reply with only the corrected JSON object."
    )


def extract_json(text: str) -> Any:
    """The last fenced JSON block, else the last top-level JSON object, else None."""
    for block in reversed(_FENCED.findall(text)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    last = None
    position = text.find("{")
    while position != -1:
        try:
            value, end = decoder.raw_decode(text, position)
        except ValueError:
            position = text.find("{", position + 1)
            continue
        last = value  # a top-level object; skip what it contains
        position = text.find("{", end)
    return last
