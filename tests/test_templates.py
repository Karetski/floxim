"""`${{ }}` templates (spec §4.4)."""

from typing import Any

import pytest

from floxim.expr import ExprError
from floxim.templates import jinja_like_offsets, parse_template, render_value

STATE: dict[str, Any] = {
    "inputs": {"n": 3, "name": "x", "flag": True, "items": [1, 2], "cfg": {"a": 1}},
    "nodes": {"a": None},
}


def render(text: str) -> Any:
    return parse_template(text).render(STATE)


def test_given_whole_value_template_when_rendered_then_keeps_the_type() -> None:
    assert render("${{ inputs.items }}") == [1, 2]
    assert render("${{inputs.n}}") == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("n=${{ inputs.n }}", "n=3"),
        ("${{ inputs.name }}-${{ inputs.flag }}", "x-true"),
        ("[${{ nodes.a }}]", "[]"),
        ("items: ${{ inputs.items }}", "items: [\n  1,\n  2\n]"),
        ("${{ inputs.cfg }}\n", '{\n  "a": 1\n}\n'),
        ("literal $${{ not an expression }}", "literal ${{ not an expression }}"),
        ("no templates here", "no templates here"),
        ("dict ${{ {'k': {'v': 1}}['k'] }}!", 'dict {\n  "v": 1\n}!'),
    ],
)
def test_given_text_around_templates_when_rendered_then_values_are_interpolated_as_text(
    text: str, expected: str
) -> None:
    assert render(text) == expected


def test_given_nested_configuration_when_rendered_then_every_string_is_rendered() -> None:
    # When
    value = render_value({"a": ["${{ inputs.n }}", "x"], "b": {"c": "$${{"}}, STATE)

    # Then
    assert value == {"a": [3, "x"], "b": {"c": "${{"}}


@pytest.mark.parametrize(
    ("text", "code", "offset"),
    [
        ("start ${{ inputs.n", "E-EXPR-SYNTAX", 6),
        ("x ${{ inputs.n + }}", "E-EXPR-SYNTAX", None),
        ("${{ upper_case(x) }}", "E-EXPR-UNKNOWN-FUNCTION", 4),
        ("ok ${{ x.__class__ }}", "E-EXPR-FORBIDDEN", None),
    ],
)
def test_given_bad_template_when_parsed_then_raises_with_code_and_offset(
    text: str, code: str, offset: int | None
) -> None:
    with pytest.raises(ExprError) as excinfo:
        parse_template(text)
    assert excinfo.value.code == code
    if offset is not None:
        assert excinfo.value.offset == offset


def test_given_jinja_style_braces_when_linted_then_their_offsets_are_reported() -> None:
    assert jinja_like_offsets("Plan: {{ nodes.plan.output }}") == [6]
    assert jinja_like_offsets("ok ${{ inputs.n }} and $${{ literal") == []
