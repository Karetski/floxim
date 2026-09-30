"""The expression language (spec §4.1, §4.2)."""

import datetime
import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from floxim.expr import EvalError, ExprError, parse

STATE: dict[str, Any] = {
    "inputs": {"feature": "login", "max_attempts": 3, "mode": "careful"},
    "nodes": {
        "test": {"exit_code": 1, "stdout": "a\nb\nc", "stderr": "boom", "outcome": "failed"},
        "plan": {"output": {"status": "ready", "steps": ["x", "y"], "summary": "Do it"}},
        "checks": {"output": [{"bucket": "pass"}, {"bucket": "fail"}]},
        "review": None,
    },
    "visits": {"implement": 2, "test": 2},
    "vars": {},
}


def ev(text: str) -> Any:
    return parse(text).evaluate(STATE)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nodes.test.exit_code == 0", False),
        ("visits.implement < 3", True),
        ("nodes.plan.output.status == 'ready' and visits.test <= 2", True),
        ("nodes['plan'].output.steps[0]", "x"),
        ("nodes.plan.output.steps[-1]", "y"),
        ("nodes.plan.output.steps[0:1]", ["x"]),
        ("1 < visits.test < 3", True),
        ("'fail' in pluck(nodes.checks.output, 'bucket')", True),
        ("'pending' not in pluck(nodes.checks.output, 'bucket')", True),
        ("nodes.review is None", True),
        ("nodes.review is not null", False),
        ("true and not false", True),
        ("null", None),
        ("'a' if visits.test == 2 else 'b'", "a"),
        ("[1, 2] + [3]", [1, 2, 3]),
        ("{'a': 1, 'b': [True, None]}", {"a": 1, "b": [True, None]}),
        ("(1, 2)", [1, 2]),
        ("7 // 2 + 7 % 2 - -1", 5),
        ("10 / 4", 2.5),
        ("'ab' * 2", "abab"),
        ("inputs.feature + '!'", "login!"),
    ],
)
def test_given_state_when_expression_evaluated_then_returns_python_semantics(
    text: str, expected: Any
) -> None:
    assert ev(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "nodes.missing.output.status",
        "nodes.review.output",
        "nodes.plan.output.nope",
        "nodes.plan.output.steps[99]",
        "undefined_root.x",
        "nodes['review']['x']",
    ],
)
def test_given_missing_data_when_read_then_navigation_is_null_safe(text: str) -> None:
    assert ev(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("len(nodes.plan.output.steps)", 2),
        ("len('abc')", 3),
        ("str(True)", "true"),
        ("str(None)", ""),
        ("int('42')", 42),
        ("float(2)", 2.0),
        ("bool('')", False),
        ("abs(-2)", 2),
        ("min([3, 1, 2])", 1),
        ("max(3, 9)", 9),
        ("round(2.567, 2)", 2.57),
        ("round(2.5)", 2),
        ("sum([1, 2, 3])", 6),
        ("sorted([3, 1])", [1, 3]),
        ("any([False, True])", True),
        ("all([])", True),
        ("default(nodes.review, 'none')", "none"),
        ("default('', 'x', empty=True)", "x"),
        ("default('', 'x')", ""),
        ("json({'a': 1}, indent=None)", '{"a": 1}'),
        ("from_json('[1, 2]')", [1, 2]),
        ("lower('AbC')", "abc"),
        ("upper('a')", "A"),
        ("strip('  a ')", "a"),
        ("startswith('abc', 'ab')", True),
        ("endswith('abc', 'x')", False),
        ("matches('v1.2.3', '^v\\\\d+')", True),
        ("replace('a-b', '-', '+')", "a+b"),
        ("split('a,b', ',')", ["a", "b"]),
        ("join(['a', 1, True], ', ')", "a, 1, true"),
        ("truncate('abcdef', 4)", "abc…"),
        ("truncate('abc', 4)", "abc"),
        ("head(nodes.test.stdout, 2)", "a\nb"),
        ("tail(nodes.test.stdout, 2)", "b\nc"),
        ("keys({'a': 1})", ["a"]),
        ("values({'a': 1})", [1]),
        ('shq("it\'s")', "'it'\"'\"'s'"),
        ("duration('1h30m')", 5400.0),
    ],
)
def test_given_builtin_function_when_called_then_returns_documented_result(
    text: str, expected: Any
) -> None:
    assert ev(text) == expected


def test_given_injected_clock_when_now_called_then_returns_utc_iso_time() -> None:
    # Given
    clock = lambda: datetime.datetime(2026, 9, 30, 14, 15, 3, tzinfo=datetime.timezone.utc)  # noqa: E731

    # When / Then
    assert parse("now()").evaluate({}, clock) == "2026-09-30T14:15:03Z"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("__import__('os')", "E-EXPR-FORBIDDEN"),
        ("nodes.__class__", "E-EXPR-FORBIDDEN"),
        ("_private", "E-EXPR-FORBIDDEN"),
        ("'a'.upper()", "E-EXPR-FORBIDDEN"),
        ("lambda: 1", "E-EXPR-FORBIDDEN"),
        ("[x for x in nodes]", "E-EXPR-FORBIDDEN"),
        ("{x: 1 for x in nodes}", "E-EXPR-FORBIDDEN"),
        ("(x for x in nodes)", "E-EXPR-FORBIDDEN"),
        ("(y := 1)", "E-EXPR-FORBIDDEN"),
        ("len(*nodes)", "E-EXPR-FORBIDDEN"),
        ("len(**nodes)", "E-EXPR-FORBIDDEN"),
        ("f'{nodes}'", "E-EXPR-FORBIDDEN"),
        ("2 ** 100", "E-EXPR-FORBIDDEN"),
        ("{1, 2}", "E-EXPR-FORBIDDEN"),
        ("b'bytes'", "E-EXPR-FORBIDDEN"),
        ("visits.x is 3", "E-EXPR-FORBIDDEN"),
        ("'x' * " + "1" * 4000, "E-EXPR-FORBIDDEN"),
        ("eval('1')", "E-EXPR-UNKNOWN-FUNCTION"),
        ("open('/etc/passwd')", "E-EXPR-UNKNOWN-FUNCTION"),
        ("nodes.test.exit_code ==", "E-EXPR-SYNTAX"),
        ("x = 1", "E-EXPR-SYNTAX"),
        ("", "E-EXPR-SYNTAX"),
    ],
)
def test_given_forbidden_or_malformed_expression_when_parsed_then_rejected_with_code(
    text: str, code: str
) -> None:
    with pytest.raises(ExprError) as excinfo:
        parse(text)
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "text",
    [
        "1 / 0",
        "'a' + 1",
        "nodes.test.stdout + None",
        "'a' < 1",
        "len(5)",
        "nodes.plan.output.steps.first",
        "nodes.plan.output.steps['x']",
        "'x' in 5",
        "tail(nodes.review, 3)",
        "min([])",
        "from_json('{')",
        "int('x')",
        "'a' * 10000000",
        "-'a'",
        "nodes.test.exit_code[0]",
    ],
)
def test_given_bad_operands_when_evaluated_then_raises_expression_error(text: str) -> None:
    with pytest.raises(EvalError):
        ev(text)


def test_given_references_when_extracted_then_paths_stop_at_computed_keys() -> None:
    # When
    refs = parse(
        "nodes.plan.output.steps[0] == inputs['feature'] and visits[x].y > len(nodes.test.stderr)"
    ).references()

    # Then
    assert sorted(r.path for r in refs) == sorted(
        [
            ("nodes", "plan", "output", "steps", 0),
            ("inputs", "feature"),
            ("visits",),
            ("x",),
            ("nodes", "test", "stderr"),
        ]
    )


def test_given_typical_condition_when_evaluated_many_times_then_stays_fast() -> None:
    # Given
    expression = parse("nodes.test.exit_code == 0 and visits.implement < 3")
    runs = 2000

    # When
    start = time.perf_counter()
    for _ in range(runs):
        expression.evaluate(STATE)
    per_eval = (time.perf_counter() - start) / runs

    # Then: tens of microseconds; the bound is loose, to catch regressions only.
    assert per_eval < 500e-6


JSON_TYPES = (dict, list, str, int, float, bool, type(None))
FRAGMENTS = [
    "__class__",
    "__import__",
    "__globals__",
    "__builtins__",
    "_x",
    "mro",
    "os",
    "sys",
    "open",
    "eval",
    "exec",
    "nodes",
    "inputs",
    "run",
    "env",
    "len",
    "str",
    "json",
    "from_json",
    "default",
    "pluck",
    ".",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    "'a'",
    '"__"',
    "1",
    "0",
    "-1",
    "None",
    "True",
    "lambda",
    "for",
    "in",
    "if",
    "else",
    " ",
    "+",
    "*",
    "**",
    "/",
    "==",
    "not",
    "and",
    "or",
    "is",
    "getattr",
    "format",
    "__subclasses__",
    "()",
    "[0]",
    ".x",
]


@settings(max_examples=3000, deadline=None)
@given(st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=12))
def test_given_arbitrary_text_when_accepted_then_evaluation_stays_inside_json_values(
    fragments: list[str],
) -> None:
    # Given
    text = "".join(fragments)
    try:
        expression = parse(text)
    except ExprError:
        return

    # When
    try:
        result = expression.evaluate(STATE)
    except EvalError:
        return

    # Then
    def walk(value: Any) -> None:
        assert isinstance(value, JSON_TYPES), (text, type(value))
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(result)
