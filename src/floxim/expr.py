"""The expression language (spec §4.1, §4.2).

A restricted subset of Python expression syntax, parsed with `ast` and
evaluated by a whitelist evaluator over JSON values: no `eval`, no builtins, no
imports, no attribute access on Python objects. Every value an expression can
produce is a JSON value (dict, list, str, int, float, bool, None).
"""

from __future__ import annotations

import ast
import datetime
import json
import math
import operator
import re
import shlex
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from floxim.units import parse_duration

MAX_LENGTH = 4000
MAX_REGEX = 500
MAX_RESULT_ITEMS = 1_000_000  # bound on strings and lists built by + and *

ALIASES = {"true": True, "false": False, "null": None}


class ExprError(Exception):
    """A problem found while parsing: `code` is an Appendix C code."""

    def __init__(self, code: str, message: str, offset: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset  # 0-based character offset into the expression


class EvalError(Exception):
    """A run-time error (error kind `expression_error`, spec §4.1)."""


@dataclass(frozen=True)
class Expression:
    text: str
    tree: ast.expr

    def evaluate(self, namespace: Mapping[str, Any], clock: Clock | None = None) -> Any:
        return _Evaluator(namespace, clock or _utcnow).eval(self.tree)

    def references(self) -> list[Reference]:
        return _references(self.tree)

    def functions(self) -> list[str]:
        return [
            n.func.id
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]


Clock = Callable[[], datetime.datetime]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# -- parsing ---------------------------------------------------------------

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_CMPOPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.keyword,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    *_BINOPS,
    *_CMPOPS,
)
_DESCRIPTIONS = {
    ast.Lambda: "lambdas",
    ast.ListComp: "comprehensions",
    ast.SetComp: "comprehensions",
    ast.DictComp: "comprehensions",
    ast.GeneratorExp: "generator expressions",
    ast.NamedExpr: "assignment expressions",
    ast.Starred: "starred arguments",
    ast.JoinedStr: "f-strings",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.Set: "set literals",
    ast.Pow: "the ** operator",
}


def parse(text: str) -> Expression:
    """Parse and check an expression. Raises ExprError with an Appendix C code."""
    if len(text) > MAX_LENGTH:
        raise ExprError("E-EXPR-FORBIDDEN", f"expressions are limited to {MAX_LENGTH} characters")
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    if not stripped:
        raise ExprError("E-EXPR-SYNTAX", "empty expression")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        raise ExprError(
            "E-EXPR-SYNTAX", f"invalid expression: {exc.msg}", (exc.offset or 1) - 1 + lead
        ) from None
    for node in ast.walk(tree):
        try:
            _check_node(node)
        except ExprError as exc:
            exc.offset += lead
            raise
    return Expression(text, tree.body)


def _check_node(node: ast.AST) -> None:
    offset = getattr(node, "col_offset", 0)
    if not isinstance(node, _ALLOWED_NODES):
        what = _DESCRIPTIONS.get(type(node), type(node).__name__)
        raise ExprError("E-EXPR-FORBIDDEN", f"{what} are not allowed in expressions", offset)
    if isinstance(node, ast.Name) and node.id.startswith("_"):
        raise ExprError(
            "E-EXPR-FORBIDDEN", f"names starting with _ are not allowed: {node.id}", offset
        )
    if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
        raise ExprError(
            "E-EXPR-FORBIDDEN", f"attributes starting with _ are not allowed: {node.attr}", offset
        )
    if isinstance(node, ast.Constant) and not isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        raise ExprError(
            "E-EXPR-FORBIDDEN", f"{type(node.value).__name__} literals are not allowed", offset
        )
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExprError(
                "E-EXPR-FORBIDDEN",
                "only the functions of spec §4.2 can be called; method calls are not allowed",
                offset,
            )
        if node.func.id.startswith("_"):
            raise ExprError(
                "E-EXPR-FORBIDDEN", f"names starting with _ are not allowed: {node.func.id}", offset
            )
        if node.func.id not in FUNCTIONS:
            raise ExprError("E-EXPR-UNKNOWN-FUNCTION", f"unknown function {node.func.id!r}", offset)
        if any(k.arg is None for k in node.keywords):
            raise ExprError("E-EXPR-FORBIDDEN", "**kwargs are not allowed", offset)
    if isinstance(node, ast.Compare):
        for op, right in zip(node.ops, node.comparators, strict=True):
            if (
                isinstance(op, (ast.Is, ast.IsNot))
                and not (isinstance(right, ast.Constant) and right.value is None)
                and not (isinstance(right, ast.Name) and right.id == "null")
            ):
                raise ExprError("E-EXPR-FORBIDDEN", "`is` can only compare with None", offset)


# -- evaluation --------------------------------------------------------------


def _type_name(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Evaluator:
    def __init__(self, namespace: Mapping[str, Any], clock: Clock) -> None:
        self.namespace = namespace
        self.clock = clock

    def eval(self, node: ast.expr) -> Any:
        method = getattr(self, f"_{type(node).__name__}")
        return method(node)

    def _Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _Name(self, node: ast.Name) -> Any:
        if node.id in ALIASES:
            return ALIASES[node.id]
        return self.namespace.get(node.id)

    def _Attribute(self, node: ast.Attribute) -> Any:
        return _get(self.eval(node.value), node.attr, node.attr)

    def _Subscript(self, node: ast.Subscript) -> Any:
        target = self.eval(node.value)
        if isinstance(node.slice, ast.Slice):
            if target is None:
                return None
            if not isinstance(target, (str, list)):
                raise EvalError(f"cannot slice a {_type_name(target)}")
            bounds = [
                None if part is None else self.eval(part)
                for part in (node.slice.lower, node.slice.upper, node.slice.step)
            ]
            for bound in bounds:
                if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool)):
                    raise EvalError("slice bounds must be integers")
            if bounds[2] == 0:
                raise EvalError("slice step cannot be zero")
            return target[bounds[0] : bounds[1] : bounds[2]]
        return _get(target, self.eval(node.slice), None)

    def _List(self, node: ast.List) -> Any:
        return [self.eval(e) for e in node.elts]

    def _Tuple(self, node: ast.Tuple) -> Any:
        return [self.eval(e) for e in node.elts]

    def _Dict(self, node: ast.Dict) -> Any:
        out: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise EvalError("** in dict literals is not allowed")
            key = self.eval(key_node)
            if not isinstance(key, str):
                raise EvalError(f"object keys must be strings, got {_type_name(key)}")
            out[key] = self.eval(value_node)
        return out

    def _BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.eval(node.left), self.eval(node.right)
        op = type(node.op)
        symbol = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.FloorDiv: "//",
            ast.Mod: "%",
        }[op]
        ok = (
            (_is_number(left) and _is_number(right))
            or (op is ast.Add and isinstance(left, str) and isinstance(right, str))
            or (op is ast.Add and isinstance(left, list) and isinstance(right, list))
            or (op is ast.Mult and isinstance(left, (str, list)) and _is_int(right))
            or (op is ast.Mult and _is_int(left) and isinstance(right, (str, list)))
        )
        if not ok:
            raise EvalError(f"cannot apply {symbol} to {_type_name(left)} and {_type_name(right)}")
        if op is ast.Mult and not (_is_number(left) and _is_number(right)):
            size = len(left) * right if isinstance(left, (str, list)) else len(right) * left
            if size > MAX_RESULT_ITEMS:
                raise EvalError("result too large")
        try:
            result = _BINOPS[op](left, right)
        except ZeroDivisionError:
            raise EvalError("division by zero") from None
        if isinstance(result, (str, list)) and len(result) > MAX_RESULT_ITEMS:
            raise EvalError("result too large")
        if isinstance(result, float) and not math.isfinite(result):
            raise EvalError("result is not a finite number")
        return result

    def _UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not _truthy(value)
        if not _is_number(value):
            raise EvalError(f"cannot negate a {_type_name(value)}")
        return -value if isinstance(node.op, ast.USub) else +value

    def _BoolOp(self, node: ast.BoolOp) -> Any:
        is_and = isinstance(node.op, ast.And)
        value: Any = None
        for operand in node.values:
            value = self.eval(operand)
            if _truthy(value) != is_and:
                return value
        return value

    def _Compare(self, node: ast.Compare) -> Any:
        left = self.eval(node.left)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = self.eval(right_node)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    def _IfExp(self, node: ast.IfExp) -> Any:
        return self.eval(node.body) if _truthy(self.eval(node.test)) else self.eval(node.orelse)

    def _Call(self, node: ast.Call) -> Any:
        assert isinstance(node.func, ast.Name)
        function = FUNCTIONS[node.func.id]
        args = [self.eval(a) for a in node.args]
        kwargs = {k.arg: self.eval(k.value) for k in node.keywords if k.arg is not None}
        try:
            if node.func.id == "now":
                if args or kwargs:
                    raise TypeError("now() takes no arguments")
                return self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
            return function(*args, **kwargs)
        except EvalError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, re.error) as exc:
            raise EvalError(f"{node.func.id}(): {exc}") from None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _truthy(value: Any) -> bool:
    return bool(value)


def _get(target: Any, key: Any, attr: str | None) -> Any:
    """Null-safe read: a missing key, an index out of range, or anything read
    from None yields None (spec §4.1)."""
    if target is None:
        return None
    if isinstance(target, dict):
        if not isinstance(key, str):
            raise EvalError(f"object keys are strings, got {_type_name(key)}")
        return target.get(key)
    if attr is not None:
        raise EvalError(f"cannot read .{attr} of a {_type_name(target)}")
    if isinstance(target, (list, str)):
        if not _is_int(key):
            raise EvalError(f"{_type_name(target)} indices must be integers")
        if -len(target) <= key < len(target):
            return target[key]
        return None
    raise EvalError(f"cannot index a {_type_name(target)}")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Is):
        return left is None
    if isinstance(op, ast.IsNot):
        return left is not None
    if isinstance(op, (ast.In, ast.NotIn)):
        if isinstance(right, str):
            if not isinstance(left, str):
                raise EvalError(f"cannot look for a {_type_name(left)} in a string")
            found = left in right
        elif isinstance(right, (list, dict)):
            found = left in right
        else:
            raise EvalError(f"cannot use `in` with a {_type_name(right)}")
        return found if isinstance(op, ast.In) else not found
    if isinstance(op, (ast.Eq, ast.NotEq)):
        return _CMPOPS[type(op)](left, right)
    comparable = (_is_number(left) and _is_number(right)) or (
        type(left) is type(right) and isinstance(left, (str, list))
    )
    if not comparable:
        raise EvalError(f"cannot compare {_type_name(left)} and {_type_name(right)}")
    return _CMPOPS[type(op)](left, right)


# -- functions (spec §4.2) ----------------------------------------------------


def render_text(value: Any) -> str:
    """Text for a value interpolated into a string (spec §4.4)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(value, indent=2, ensure_ascii=False)


def _need(value: Any, kind: type | tuple[type, ...], name: str, what: str) -> None:
    if not isinstance(value, kind) or (isinstance(value, bool) and bool not in _as_tuple(kind)):
        raise EvalError(f"{name}() expects {what}, got {_type_name(value)}")


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


def _len(x: Any) -> int:
    _need(x, (str, list, dict), "len", "a string, list or object")
    return len(x)


def _int(x: Any) -> int:
    if isinstance(x, str):
        return int(x.strip())
    _need(x, (int, float, bool), "int", "a number, boolean or string")
    return int(x)


def _float(x: Any) -> float:
    if isinstance(x, str):
        return float(x.strip())
    _need(x, (int, float, bool), "float", "a number, boolean or string")
    return float(x)


def _numbers(name: str, values: Any) -> list[Any]:
    _need(values, list, name, "a list")
    return list(values)


def _min(*args: Any) -> Any:
    items = _numbers("min", args[0]) if len(args) == 1 else list(args)
    if not items:
        raise EvalError("min() of an empty list")
    return min(items)


def _max(*args: Any) -> Any:
    items = _numbers("max", args[0]) if len(args) == 1 else list(args)
    if not items:
        raise EvalError("max() of an empty list")
    return max(items)


def _sum(xs: Any) -> Any:
    items = _numbers("sum", xs)
    if not all(_is_number(i) for i in items):
        raise EvalError("sum() expects a list of numbers")
    return sum(items)


def _round(x: Any, ndigits: Any = 0) -> Any:
    _need(x, (int, float), "round", "a number")
    result = round(x, ndigits)
    return int(result) if ndigits == 0 else result


def _default(x: Any, fallback: Any, empty: bool = False) -> Any:
    if x is None or (empty and x == ""):
        return fallback
    return x


def _json(x: Any, indent: Any = 2) -> str:
    return json.dumps(x, indent=indent, ensure_ascii=False)


def _from_json(s: Any) -> Any:
    _need(s, str, "from_json", "a string")
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        raise EvalError(f"from_json(): {exc}") from None


def _str_fn(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(s: Any, *args: Any) -> Any:
        _need(s, str, name, "a string")
        return fn(s, *args)

    return wrapper


def _matches(s: Any, pattern: Any) -> bool:
    _need(s, str, "matches", "a string")
    _need(pattern, str, "matches", "a regex string")
    if len(pattern) > MAX_REGEX:
        raise EvalError(f"matches(): regex longer than {MAX_REGEX} characters")
    return re.search(pattern, s) is not None


def _split(s: Any, sep: Any = None) -> list[str]:
    _need(s, str, "split", "a string")
    return list(s.split(sep))


def _join(xs: Any, sep: Any = "") -> str:
    _need(xs, list, "join", "a list")
    _need(sep, str, "join", "a string separator")
    return str(sep).join(render_text(x) for x in xs)


def _truncate(s: Any, n: Any) -> str:
    _need(s, str, "truncate", "a string")
    _need(n, int, "truncate", "an integer length")
    return str(s) if len(s) <= n else str(s[: max(n - 1, 0)]) + "…"


def _head(s: Any, n: Any) -> str:
    _need(s, str, "head", "a string")
    _need(n, int, "head", "an integer")
    return "\n".join(s.splitlines()[:n])


def _tail(s: Any, n: Any) -> str:
    _need(s, str, "tail", "a string")
    _need(n, int, "tail", "an integer")
    return "\n".join(s.splitlines()[-n:]) if n > 0 else ""


def _keys(o: Any) -> list[str]:
    _need(o, dict, "keys", "an object")
    return list(o)


def _values(o: Any) -> list[Any]:
    _need(o, dict, "values", "an object")
    return list(o.values())


def _pluck(xs: Any, key: Any) -> list[Any]:
    _need(xs, list, "pluck", "a list")
    return [x.get(key) if isinstance(x, dict) else None for x in xs]


def _sorted(xs: Any) -> list[Any]:
    return sorted(_numbers("sorted", xs))


def _any(xs: Any) -> bool:
    return any(_truthy(x) for x in _numbers("any", xs))


def _all(xs: Any) -> bool:
    return all(_truthy(x) for x in _numbers("all", xs))


def _abs(x: Any) -> Any:
    _need(x, (int, float), "abs", "a number")
    return abs(x)


def _duration(s: Any) -> float:
    seconds = parse_duration(s)
    if seconds is None:
        raise EvalError('duration(): "none" has no length')
    return seconds


def _now() -> str:  # evaluated with the evaluator's clock; see _Evaluator._Call
    raise AssertionError("unreachable")


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": _len,
    "str": render_text,
    "int": _int,
    "float": _float,
    "bool": _truthy,
    "abs": _abs,
    "min": _min,
    "max": _max,
    "round": _round,
    "sum": _sum,
    "sorted": _sorted,
    "any": _any,
    "all": _all,
    "default": _default,
    "json": _json,
    "from_json": _from_json,
    "lower": _str_fn("lower", str.lower),
    "upper": _str_fn("upper", str.upper),
    "strip": _str_fn("strip", str.strip),
    "startswith": _str_fn("startswith", str.startswith),
    "endswith": _str_fn("endswith", str.endswith),
    "matches": _matches,
    "replace": _str_fn("replace", str.replace),
    "split": _split,
    "join": _join,
    "truncate": _truncate,
    "head": _head,
    "tail": _tail,
    "keys": _keys,
    "values": _values,
    "pluck": _pluck,
    "shq": _str_fn("shq", shlex.quote),
    "now": _now,
    "duration": _duration,
}


# -- static references ---------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """A state path read by an expression: `nodes.plan.output.status` is
    ("nodes", "plan", "output", "status"). The path stops at the first part that
    is not a literal name or key."""

    path: tuple[str | int, ...]
    offset: int


def _references(tree: ast.expr) -> list[Reference]:
    found: list[Reference] = []
    consumed: set[int] = {
        id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
    }  # function names are not state
    for node in ast.walk(tree):
        if id(node) in consumed or not isinstance(node, (ast.Attribute, ast.Subscript, ast.Name)):
            continue
        path: list[str | int] = []
        current: ast.expr = node
        chain: list[ast.expr] = []
        while True:
            chain.append(current)
            if isinstance(current, ast.Attribute):
                path.append(current.attr)
                current = current.value
            elif isinstance(current, ast.Subscript):
                key = current.slice
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, (str, int))
                    and not (isinstance(key.value, bool))
                ):
                    path.append(key.value)
                else:
                    path = []  # a computed key: keep only the part before it
                current = current.value
            elif isinstance(current, ast.Name):
                if current.id in ALIASES:
                    break
                path.append(current.id)
                path.reverse()
                found.append(Reference(tuple(path), node.col_offset))
                consumed.update(id(c) for c in chain)
                break
            else:
                break
    return found
