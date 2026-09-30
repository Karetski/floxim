"""Static checks on a loaded flow: graph (§3.9, §6.4, §6.5), expressions and
references (§4.5), and security lints (§12.7)."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow.config import find_git_root
from arcflow.expr import ExprError, Expression, Reference, parse
from arcflow.flow import Flow, Node, Origin
from arcflow.flowspec import (
    BUDGET_FIELDS,
    COMMON_RESULT_FIELDS,
    NODE_CONTEXT_FIELDS,
    RESULT_FIELDS,
    RUN_FIELDS,
    SCHEMA_OUTPUT_TYPES,
    TERMINAL_TARGETS,
    node_fields,
)
from arcflow.graph import END, Graph
from arcflow.problems import Problem, join_pointer
from arcflow.schema import AnyValue, ListOf, MapOf, OneOf, Struct, Type, closest
from arcflow.templates import jinja_like_offsets, parse_template

STATE_ROOTS = frozenset(
    {"inputs", "nodes", "visits", "vars", "run", "env", "node", "self", "item", "index"}
)


# -- graph -------------------------------------------------------------------


def graph_checks(flow: Flow, graph: Graph) -> list[Problem]:
    problems: list[Problem] = []
    known = set(flow.nodes) | TERMINAL_TARGETS
    if flow.start not in flow.nodes:
        problems.append(
            flow.doc.problem(
                "E-UNKNOWN-START", f"start node {flow.start!r} does not exist", "/start"
            )
        )
    reported: set[tuple[str, str]] = set()
    for edge in graph.edges:
        key = (edge.origin.doc.file, edge.origin.pointer)
        if edge.target not in known and key not in reported:
            reported.add(key)
            hint = closest(edge.target, known)
            problems.append(
                edge.origin.problem(
                    "E-UNKNOWN-TARGET",
                    f"no node named {edge.target!r}"
                    + (f" (did you mean {hint!r}?)" if hint else ""),
                )
            )
    for node in flow.nodes.values():
        problems += _case_order(node, "next")
        problems += _case_order(node, "on_error")
        next_value = node.config.get("next")
        has_default = isinstance(next_value, list) and any("when" not in c for c in next_value)
        if node.type == "condition" and not has_default:
            problems.append(
                node.where("next").problem(
                    "E-CONDITION-NEEDS-DEFAULT",
                    "a condition node's next must be a list of cases ending with a default",
                )
            )
        elif isinstance(next_value, list) and not has_default:
            problems.append(
                node.where("next").problem(
                    "W-NO-DEFAULT-ROUTE",
                    "no default case: the run fails with E-NO-ROUTE if no case matches",
                )
            )
    if any(p.is_error for p in problems):
        return problems

    reachable = graph.reachable()
    for node in flow.nodes.values():
        if node.id not in reachable:
            problems.append(
                node.origin.problem(
                    "W-UNREACHABLE", f"node {node.id!r} is not reachable from start", key=True
                )
            )
    exits = _exit_graph(graph)
    for node in flow.nodes.values():
        if not exits.reachable_from(node.id) & TERMINAL_TARGETS:
            problems.append(
                node.origin.problem(
                    "E-NO-EXIT",
                    f"neither end nor fail can be reached from {node.id!r}; the run could "
                    "only stop at a limit",
                    key=True,
                )
            )
    problems += _cycle_guards(flow, graph)
    problems += _error_never_routed(flow)
    return problems


def _exit_graph(graph: Graph) -> Graph:
    """Edges that can finish a run as written: every `next` edge (including the
    implicit `next: end`) and `on_error` edges the author wrote."""
    return Graph(
        graph.start, graph.nodes, [e for e in graph.edges if e.via == "next" or e.explicit]
    )


def _case_order(node: Node, key: str) -> list[Problem]:
    value = node.config.get(key)
    if not isinstance(value, list):
        return []
    origin = node.where(key)
    defaults = [i for i, case in enumerate(value) if "when" not in case]
    problems = []
    if len(defaults) > 1:
        problems.append(
            Origin(origin.doc, join_pointer(origin.pointer, defaults[1])).problem(
                "E-MULTIPLE-DEFAULTS", f"{key} has more than one case without `when`"
            )
        )
    elif defaults and defaults[0] != len(value) - 1:
        problems.append(
            Origin(origin.doc, join_pointer(origin.pointer, defaults[0])).problem(
                "E-DEFAULT-NOT-LAST", "the default case (without `when`) must be last"
            )
        )
    return problems


def _routing_expressions(node: Node) -> Iterator[tuple[str, Origin]]:
    for key in ("next", "on_error"):
        value = node.config.get(key)
        if not isinstance(value, list):
            continue
        origin = node.where(key)
        for index, case in enumerate(value):
            if isinstance(case.get("when"), str):
                pointer = join_pointer(join_pointer(origin.pointer, index), "when")
                yield case["when"], Origin(origin.doc, pointer)


def _parsed(text: str) -> Expression | None:
    try:
        return parse(text)
    except ExprError:
        return None


def _cycle_guards(flow: Flow, graph: Graph) -> list[Problem]:
    problems = []
    for component in graph.cycles():
        guarded = False
        for node_id in component:
            for text, _ in _routing_expressions(flow.nodes[node_id]):
                expression = _parsed(text)
                if expression is None:
                    continue
                for ref in expression.references():
                    if (
                        ref.path[:1] == ("visits",)
                        and len(ref.path) > 1
                        and ref.path[1] in component
                    ):
                        guarded = True
        if not guarded:
            first = next(n for n in flow.nodes if n in component)
            members = ", ".join(n for n in flow.nodes if n in component)
            problems.append(
                flow.nodes[first].origin.problem(
                    "W-UNGUARDED-CYCLE",
                    f"the loop through {members} has no `when:` on visits of its nodes; it "
                    "stops only at max_visits",
                    key=True,
                )
            )
    return problems


def _error_never_routed(flow: Flow) -> list[Problem]:
    problems = []
    for node in flow.nodes.values():
        if node.config.get("on_error", "fail") != "fail":
            continue
        for text, origin in _routing_expressions(node):
            expression = _parsed(text)
            if expression is None:
                continue
            for ref in expression.references():
                own = (
                    ref.path[:2] == ("nodes", node.id)
                    and ref.path[2:3] in (("exit_code",), ("outcome",))
                ) or (ref.path[:1] == ("self",) and ref.path[1:2] in (("exit_code",), ("outcome",)))
                if own:
                    problems.append(
                        origin.problem(
                            "W-ERROR-NEVER-ROUTED",
                            f"next inspects {'.'.join(map(str, ref.path))}, but on_error is "
                            "fail, so a failed visit never gets here; set on_error: continue",
                        )
                    )
                    break
            else:
                continue
            break
    return problems


# -- expressions and references ----------------------------------------------


@dataclass(frozen=True)
class _Site:
    """Where an expression is evaluated, for reference checks."""

    node: Node | None  # None for flow `outputs`
    routing: bool  # in the node's own next/on_error
    in_map_inputs: bool = False


def _templated_strings(value: Any, type_: Type, origin: Origin) -> Iterator[tuple[str, Origin]]:
    """Every string inside `value` that the declaration marks as a template."""
    if isinstance(value, str):
        if getattr(type_, "templated", False):
            yield value, origin
        return
    if isinstance(type_, OneOf):
        for alt in type_.alternatives:
            if (isinstance(value, dict) and isinstance(alt, (Struct, MapOf))) or (
                isinstance(value, list) and isinstance(alt, ListOf)
            ):
                yield from _templated_strings(value, alt, origin)
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
                yield from _templated_strings(
                    element, item, Origin(origin.doc, join_pointer(origin.pointer, index))
                )
    elif isinstance(value, dict):
        for key, element in value.items():
            child = Origin(origin.doc, join_pointer(origin.pointer, key))
            if isinstance(type_, Struct) and key in type_.fields:
                yield from _templated_strings(element, type_.fields[key].type, child)
            elif isinstance(type_, MapOf):
                yield from _templated_strings(element, type_.value, child)
            elif isinstance(type_, AnyValue):
                yield from _templated_strings(element, type_, child)


def expression_checks(flow: Flow, graph: Graph) -> list[Problem]:
    checker = _ReferenceChecker(flow, graph)
    for node in flow.nodes.values():
        fields = node_fields(node.type)
        for key, value in node.config.items():
            if key in ("next", "on_error") or key not in fields:
                continue
            site = _Site(node, routing=False, in_map_inputs=node.type == "map" and key == "inputs")
            for text, origin in _templated_strings(value, fields[key].type, node.where(key)):
                checker.template(text, origin, site)
        for text, origin in _routing_expressions(node):
            checker.expression(text, origin, _Site(node, routing=True))
        for key in ("next", "on_error"):
            value = node.config.get(key)
            if isinstance(value, list):
                origin = node.where(key)
                for index, case in enumerate(value):
                    if isinstance(case.get("reason"), str):
                        pointer = join_pointer(join_pointer(origin.pointer, index), "reason")
                        checker.template(
                            case["reason"], Origin(origin.doc, pointer), _Site(node, routing=True)
                        )
    for name, value in flow.outputs.items():
        origin = Origin(flow.doc, join_pointer("/outputs", name))
        for text, where in _templated_strings(value, AnyValue(templated=True), origin):
            checker.template(text, where, _Site(None, routing=False))
    return checker.problems


class _ReferenceChecker:
    def __init__(self, flow: Flow, graph: Graph) -> None:
        self.flow = flow
        self.graph = graph
        self.problems: list[Problem] = []
        self.unchecked_reported: set[str] = set()

    def template(self, text: str, origin: Origin, site: _Site) -> None:
        try:
            template = parse_template(text)
        except ExprError as exc:
            self.problems.append(origin.problem(exc.code, exc.message))
            return
        if jinja_like_offsets(text):
            self.problems.append(
                origin.problem(
                    "W-JINJA-LIKE",
                    "`{{ }}` is not template syntax here; write `${{ expression }}`",
                )
            )
        for part in template.expressions:
            assert part.expression is not None
            self._references(part.expression, origin, site)

    def expression(self, text: str, origin: Origin, site: _Site) -> None:
        try:
            expression = parse(text)
        except ExprError as exc:
            self.problems.append(origin.problem(exc.code, exc.message))
            return
        self._references(expression, origin, site)

    def _references(self, expression: Expression, origin: Origin, site: _Site) -> None:
        for ref in expression.references():
            message = self._check(ref, site)
            if message is not None:
                self.problems.append(origin.problem(message[0], message[1]))

    def _check(self, ref: Reference, site: _Site) -> tuple[str, str] | None:
        path = ref.path
        root = path[0]
        shown = ".".join(str(p) for p in path)
        if root not in STATE_ROOTS:
            hint = closest(str(root), STATE_ROOTS)
            return "E-UNKNOWN-REF", f"unknown name {root!r}" + (
                f" (did you mean {hint!r}?)" if hint else ""
            )
        if root == "self":
            if site.node is None or not site.routing:
                return "E-UNKNOWN-REF", "`self` can only be used in a node's next and on_error"
            return self._node_ref(("nodes", site.node.id, *path[1:]), site, shown)
        if root == "node" and site.node is None:
            return "E-UNKNOWN-REF", "`node` can only be used inside a node's configuration"
        if root in ("item", "index") and not site.in_map_inputs:
            return "E-UNKNOWN-REF", f"`{root}` is only available in a map node's inputs"
        if len(path) < 2:
            return None
        name = path[1]
        if root == "inputs" and name not in self.flow.inputs:
            return _unknown("input", str(name), self.flow.inputs)
        if root == "visits" and name not in self.flow.nodes:
            return _unknown("node", str(name), self.flow.nodes)
        if root == "run":
            if name not in RUN_FIELDS:
                return _unknown("run field", str(name), RUN_FIELDS)
            if name == "budget" and len(path) > 2 and path[2] not in BUDGET_FIELDS:
                return _unknown("budget field", str(path[2]), BUDGET_FIELDS)
        if root == "node" and name not in NODE_CONTEXT_FIELDS:
            return _unknown("node field", str(name), NODE_CONTEXT_FIELDS)
        if root == "nodes":
            return self._node_ref(path, site, shown)
        return None

    def _node_ref(
        self, path: tuple[str | int, ...], site: _Site, shown: str
    ) -> tuple[str, str] | None:
        if len(path) < 2:
            return None
        node_id = str(path[1])
        target = self.flow.nodes.get(node_id)
        if target is None:
            return _unknown("node", node_id, self.flow.nodes)
        rest = list(path[2:])
        if rest and rest[0] == "visits":
            rest = rest[2:] if len(rest) > 1 and isinstance(rest[1], int) else []
        if rest:
            field_name = rest[0]
            allowed = COMMON_RESULT_FIELDS | RESULT_FIELDS[target.type]
            if field_name not in allowed:
                return _unknown(f"{target.type} result field", str(field_name), allowed)
            if field_name == "output" and len(rest) > 1 and target.type in SCHEMA_OUTPUT_TYPES:
                problem = self._output_path(target, rest[1:], shown)
                if problem is not None:
                    return problem
        if not self._can_have_run(target.id, site):
            return (
                "W-NEVER-SET",
                f"{shown} is always None here: {target.id!r} cannot have run before this point",
            )
        return None

    def _output_path(
        self, target: Node, path: list[str | int], shown: str
    ) -> tuple[str, str] | None:
        schema = self.flow.schema_for(target, "output_schema")
        if schema is None:
            if target.id not in self.unchecked_reported:
                self.unchecked_reported.add(target.id)
                return (
                    "I-UNCHECKED-OUTPUT",
                    f"{target.id!r} has no output_schema, so {shown} is not checked",
                )
            return None
        missing = schema_path_problem(schema, path)
        if missing is not None:
            return "E-UNKNOWN-REF", f"{shown}: {missing} in {target.id!r}'s output_schema"
        return None

    def _can_have_run(self, node_id: str, site: _Site) -> bool:
        if site.node is None:  # flow outputs are evaluated on reaching end
            return self.graph.has_path(node_id, END)
        if node_id == site.node.id and site.routing:
            return True
        return self.graph.has_path(node_id, site.node.id)


def _unknown(what: str, name: str, options: Any) -> tuple[str, str]:
    hint = closest(name, [str(o) for o in options])
    return "E-UNKNOWN-REF", f"no {what} named {name!r}" + (
        f" (did you mean {hint!r}?)" if hint else ""
    )


def schema_path_problem(schema: Any, path: list[str | int]) -> str | None:
    """Why `path` cannot exist in values matching `schema`, or None if it can
    (or the schema does not say)."""
    return _walk_schema(schema, schema, path, 0)


def _walk_schema(schema: Any, root: Any, path: list[str | int], depth: int) -> str | None:
    if depth > 32 or not path or not isinstance(schema, dict):
        return None
    if isinstance(schema.get("$ref"), str) and schema["$ref"].startswith("#"):
        resolved: Any = root
        for token in schema["$ref"][1:].split("/")[1:]:
            resolved = (
                resolved.get(token.replace("~1", "/").replace("~0", "~"))
                if isinstance(resolved, dict)
                else None
            )
        return _walk_schema(resolved, root, path, depth + 1)
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            results = [_walk_schema(s, root, path, depth + 1) for s in schema[key]]
            return None if any(r is None for r in results) else results[0]
    if isinstance(schema.get("allOf"), list):
        for sub in schema["allOf"]:
            result = _walk_schema(sub, root, path, depth + 1)
            if result is not None:
                return result
    token, rest = path[0], path[1:]
    if isinstance(token, int):
        items = schema.get("items")
        if isinstance(items, dict):
            return _walk_schema(items, root, rest, depth + 1)
        if isinstance(items, list):
            return (
                _walk_schema(items[token], root, rest, depth + 1)
                if -len(items) <= token < len(items)
                else f"no item {token}"
            )
        return None
    properties = schema.get("properties")
    if isinstance(properties, dict) and token in properties:
        return _walk_schema(properties[token], root, rest, depth + 1)
    if schema.get("additionalProperties") is False and not schema.get("patternProperties"):
        return f"no property {token!r}"
    return None


# -- lints -----------------------------------------------------------------


def lint_checks(flow: Flow, graph: Graph, risky_commands: list[str]) -> list[Problem]:
    problems: list[Problem] = []
    limits = flow.limits
    for key in ("max_steps", "max_duration"):
        if limits.get(key) == "none":
            problems.append(
                flow.doc.problem("W-UNBOUNDED", f"limits.{key} is disabled", f"/limits/{key}")
            )
    for key in ("usd", "tokens"):
        if (limits.get("budget") or {}).get(key) == "none":
            problems.append(
                flow.doc.problem(
                    "W-UNBOUNDED", f"limits.budget.{key} is disabled", f"/limits/budget/{key}"
                )
            )
    risky = [re.compile(p) for p in risky_commands]
    unguarded = _reachable_without_human(flow, graph)
    for node in flow.nodes.values():
        if node.type == "agent" and node.config.get("permissions") == "full":
            problems.append(
                node.where("permissions").problem(
                    "W-FULL-PERMISSIONS",
                    "permissions: full lets the agent do anything you can; arcflow run "
                    "refuses it without --allow-full",
                )
            )
        if node.type != "shell":
            continue
        run = node.config.get("run")
        if isinstance(run, str):
            problems += _shell_interpolation(run, node.where("run"))
        command = run if isinstance(run, str) else " ".join(map(str, node.config.get("args", [])))
        if node.id in unguarded and any(p.search(command) for p in risky):
            problems.append(
                node.origin.problem(
                    "W-NO-HUMAN-BEFORE-RISKY",
                    f"{node.id!r} runs a risky command and can be reached without passing "
                    "a human node",
                    key=True,
                )
            )
    return problems


def _shell_interpolation(run: str, origin: Origin) -> list[Problem]:
    try:
        template = parse_template(run)
    except ExprError:
        return []
    for part in template.expressions:
        assert part.expression is not None
        tree = part.expression.tree
        quoted = (
            isinstance(tree, ast.Call) and isinstance(tree.func, ast.Name) and tree.func.id == "shq"
        )
        tainted = any(r.path[:1] in (("nodes",), ("vars",)) for r in part.expression.references())
        if tainted and not quoted:
            return [
                origin.problem(
                    "W-SHELL-INTERPOLATION",
                    f"`{part.text}` puts node output into a shell script; wrap it in shq(), "
                    "pass it through env:, or use args:",
                )
            ]
    return []


def _reachable_without_human(flow: Flow, graph: Graph) -> set[str]:
    seen: set[str] = set()
    stack = [flow.start]
    while stack:
        current = stack.pop()
        if current in seen or current not in flow.nodes:
            continue
        seen.add(current)
        if flow.nodes[current].type == "human":
            continue
        stack.extend(graph.successors(current))
    return {n for n in seen if flow.nodes[n].type != "human"}


def workspace_checks(flow: Flow, workdir: Path) -> list[Problem]:
    if find_git_root(workdir) is not None:
        return []
    problems = []
    for node in flow.nodes.values():
        workspace = node.config.get("workspace")
        if workspace == "worktree" or isinstance(workspace, dict):
            problems.append(
                node.where("workspace").problem(
                    "E-WORKSPACE-NO-GIT",
                    f"worktrees need a git repository, and {workdir} is not inside one",
                )
            )
    return problems
