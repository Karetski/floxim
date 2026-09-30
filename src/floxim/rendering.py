"""Which strings in a node's configuration are templates, and rendering them.

The declarations in `floxim.flowspec` mark templated fields (spec §5, "T");
validation parses exactly those strings, and the runner renders exactly those.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from floxim.expr import Clock
from floxim.flow import Origin
from floxim.flowspec import ROUTING_KEYS, node_fields
from floxim.problems import join_pointer
from floxim.schema import AnyValue, ListOf, MapOf, OneOf, Struct, Type
from floxim.templates import OPEN, parse_template


def templated_strings(value: Any, type_: Type, origin: Origin) -> Iterator[tuple[str, Origin]]:
    """Every string inside `value` that the declaration marks as a template."""
    for text, pointer in _walk(value, type_, origin.pointer):
        yield text, Origin(origin.doc, pointer)


def _walk(value: Any, type_: Type, pointer: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        if getattr(type_, "templated", False):
            yield value, pointer
        return
    if isinstance(type_, OneOf):
        for alt in type_.alternatives:
            if (isinstance(value, dict) and isinstance(alt, (Struct, MapOf))) or (
                isinstance(value, list) and isinstance(alt, ListOf)
            ):
                yield from _walk(value, alt, pointer)
                return
        return
    if isinstance(value, list):
        item: Type | None = (
            type_.item
            if isinstance(type_, ListOf)
            else (type_ if isinstance(type_, AnyValue) else None)
        )
        if item is not None:
            for index, element in enumerate(value):
                yield from _walk(element, item, join_pointer(pointer, index))
    elif isinstance(value, dict):
        for key, element in value.items():
            child = join_pointer(pointer, key)
            if isinstance(type_, Struct) and key in type_.fields:
                yield from _walk(element, type_.fields[key].type, child)
            elif isinstance(type_, MapOf):
                yield from _walk(element, type_.value, child)
            elif isinstance(type_, AnyValue):
                yield from _walk(element, type_, child)


# Fields rendered later by their executor: a map node's inputs, once per item.
DEFERRED = frozenset({("map", "inputs")})


def render_config(
    node_type: str,
    config: Mapping[str, Any],
    namespace: Mapping[str, Any],
    clock: Clock | None = None,
) -> dict[str, Any]:
    """The node's configuration with every templated field rendered. Routing
    (`next`, `on_error`) is left as written; it is evaluated after the visit."""
    fields = node_fields(node_type)
    rendered: dict[str, Any] = {}
    for key, value in config.items():
        if key in ROUTING_KEYS or key not in fields or (node_type, key) in DEFERRED:
            rendered[key] = value
        else:
            rendered[key] = _render(value, fields[key].type, namespace, clock)
    return rendered


def _render(value: Any, type_: Type, namespace: Mapping[str, Any], clock: Clock | None) -> Any:
    if isinstance(value, str):
        if getattr(type_, "templated", False) and OPEN in value:
            return parse_template(value).render(namespace, clock)
        return value
    if isinstance(type_, OneOf):
        for alt in type_.alternatives:
            if (isinstance(value, dict) and isinstance(alt, (Struct, MapOf))) or (
                isinstance(value, list) and isinstance(alt, ListOf)
            ):
                return _render(value, alt, namespace, clock)
        return value
    if isinstance(value, list):
        item = (
            type_.item
            if isinstance(type_, ListOf)
            else type_
            if isinstance(type_, AnyValue)
            else None
        )
        if item is None:
            return value
        return [_render(v, item, namespace, clock) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, element in value.items():
            if isinstance(type_, Struct) and key in type_.fields:
                out[key] = _render(element, type_.fields[key].type, namespace, clock)
            elif isinstance(type_, MapOf):
                out[key] = _render(element, type_.value, namespace, clock)
            elif isinstance(type_, AnyValue):
                out[key] = _render(element, type_, namespace, clock)
            else:
                out[key] = element
        return out
    return value
