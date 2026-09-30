"""Static checks on a loaded flow: graph (§3.9, §6.4, §6.5), expressions and
references (§4.5), and security lints (§12.7)."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.config import find_git_root
from floxim.expr import ExprError, Expression, Reference, parse
from floxim.flow import Flow, Node, Origin, load_flow
from floxim.flowspec import (
    BUDGET_FIELDS,
    COMMON_RESULT_FIELDS,
    MAX_SLEEP,
    NODE_CONTEXT_FIELDS,
    RESULT_FIELDS,
    RUN_FIELDS,
    SCHEMA_OUTPUT_TYPES,
    TERMINAL_TARGETS,
    node_fields,
)
from floxim.graph import END, Graph
from floxim.problems import Problem, join_pointer
from floxim.rendering import templated_strings
from floxim.schema import AnyValue, closest
from floxim.templates import has_template, jinja_like_offsets, parse_template
from floxim.units import parse_duration

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


def expression_checks(flow: Flow, graph: Graph) -> list[Problem]:
    checker = _ReferenceChecker(flow, graph)
    for node in flow.nodes.values():
        fields = node_fields(node.type)
        for key, value in node.config.items():
            if key in ("next", "on_error") or key not in fields:
                continue
            site = _Site(node, routing=False, in_map_inputs=node.type == "map" and key == "inputs")
            for text, origin in templated_strings(value, fields[key].type, node.where(key)):
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
        for text, where in templated_strings(value, AnyValue(templated=True), origin):
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
                    "permissions: full lets the agent do anything you can; floxim run "
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


def limit_checks(flow: Flow) -> list[Problem]:
    """A sleep with a fixed duration longer than the run may last (spec §3.4, §5.5)."""
    max_duration = flow.limits.get("max_duration", "8h")
    limit = parse_duration(max_duration)
    ceiling = max(parse_duration(MAX_SLEEP) or 0, limit or 0) if limit is not None else None
    problems = []
    for node in flow.nodes.values():
        duration = node.config.get("duration")
        if node.type != "sleep" or duration is None or has_template(duration):
            continue
        seconds = parse_duration(duration) or 0
        if limit is not None and seconds > limit:
            problems.append(
                node.where("duration").problem(
                    "E-SCHEMA", f"sleeps longer than limits.max_duration ({max_duration})"
                )
            )
        elif ceiling is not None and seconds > ceiling:
            problems.append(
                node.where("duration").problem("E-SCHEMA", f"sleeps are limited to {MAX_SLEEP}")
            )
    return problems


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


# -- harnesses and capabilities (§8.1, §8.2) -------------------------------------


def harness_checks(
    flow: Flow, prices: dict[str, Any] | None = None, root: Path | None = None
) -> list[Problem]:
    from floxim import jsonschemas
    from floxim.adapters import registry

    problems: list[Problem] = []
    for node in flow.nodes.values():
        if node.type != "agent":
            continue
        config = node.config
        harness = config.get("harness")
        if not isinstance(harness, str):
            continue
        where = node.where("harness")
        if not registry.is_known(harness, root):
            hint = closest(harness, registry.names(root))
            problems.append(
                where.problem(
                    "E-UNKNOWN-HARNESS",
                    f"no adapter named {harness!r}"
                    + (f" (did you mean {hint!r}?)" if hint else ""),
                )
            )
            continue
        problems += _session_source(flow, node)
        if harness in registry.PENDING or registry.is_aap(harness, root):
            continue  # reported as E-NOT-IMPLEMENTED by the last stage
        file_problems = registry.file_problems(harness, root)
        problems += file_problems
        if any(p.is_error for p in file_problems):
            continue
        adapter = registry.load(harness, root)
        caps = adapter.capabilities()
        options = config.get("harness_options") or {}
        error = jsonschemas.validation_error(
            jsonschemas.compile_schema(adapter.options_schema()), options
        )
        if error is not None:
            problems.append(node.where("harness_options").problem("E-HARNESS-OPTIONS", error))
        if "extra_args" in options:
            problems.append(
                node.where("harness_options").problem(
                    "I-EXTRA-ARGS", "harness_options.extra_args passes arguments straight through"
                )
            )
        permissions = config.get("permissions", "edit")
        if permissions not in caps.permission_profiles:
            problems.append(
                node.where("permissions").problem(
                    "E-PERMISSION-UNSUPPORTED",
                    f"the {harness} adapter has no {permissions!r} permission profile",
                )
            )
        ignored = []
        if "effort" in config and not caps.effort:
            ignored.append("effort")
        if ("allow_tools" in config or "deny_tools" in config) and not caps.tool_rules:
            ignored.append("allow_tools/deny_tools")
        if config.get("bare") and not caps.bare:
            ignored.append("bare")
        if "max_turns" in config and not caps.turn_cap:
            ignored.append("max_turns")
        session = config.get("session", "new")
        if session != "new" and not caps.resume:
            ignored.append("session (sessions cannot be resumed; each attempt starts new)")
        elif isinstance(session, dict) and "fork" in session and not caps.fork:
            ignored.append("session.fork (the session is resumed instead)")
        usd = (config.get("budget") or {}).get("usd")
        if usd not in (None, "none") and not caps.cost_usd and not (prices or {}).get(harness):
            problems.append(
                node.where("budget").problem(
                    "W-USD-UNENFORCEABLE",
                    f"the {harness} adapter reports no cost and no price is configured for it; "
                    "only the tokens budget is enforced",
                )
            )
        if harness == "codex":
            schema = flow.schema_for(node, "output_schema")
            loose = strict_schema_problem(schema) if schema is not None else None
            if loose is not None:
                problems.append(
                    node.where("output_schema").problem(
                        "W-CODEX-STRICT-SCHEMA",
                        f"Codex strict structured output will likely reject this schema: {loose}",
                    )
                )
        for what in ignored:
            problems.append(
                node.origin.problem(
                    "W-IGNORED-OPTION", f"the {harness} adapter ignores {what}", key=True
                )
            )
    return problems


def handoff_checks(flow: Flow, root: Path | None = None) -> list[Problem]:
    """A handoff's `from` is an agent node whose adapter can open sessions
    interactively (spec §5.10, §8.1)."""
    from floxim.adapters import registry

    problems: list[Problem] = []
    for node in flow.nodes.values():
        if node.type != "handoff":
            continue
        where = node.where("from")
        source = flow.nodes.get(str(node.config.get("from")))
        if source is None:
            problems.append(
                _unknown_problem(where, "node", str(node.config.get("from")), flow.nodes)
            )
            continue
        if source.type != "agent":
            problems.append(where.problem("E-SCHEMA", f"{source.id!r} is not an agent node"))
            continue
        harness = source.config.get("harness")
        caps = registry.capabilities(str(harness), root) if isinstance(harness, str) else None
        if caps is not None and not caps.interactive:
            problems.append(
                where.problem(
                    "E-SCHEMA",
                    f"the {harness} adapter cannot open sessions interactively",
                )
            )
    return problems


def _unknown_problem(where: Origin, what: str, name: str, options: Any) -> Problem:
    code, message = _unknown(what, name, options)
    return where.problem(code, message)


def _session_source(flow: Flow, node: Node) -> list[Problem]:
    session = node.config.get("session")
    if not isinstance(session, dict):
        return []
    key = "resume" if "resume" in session else "fork"
    source_id = session[key]
    source = flow.nodes.get(source_id)
    where = node.where("session")
    if source is None:
        return [where.problem("E-UNKNOWN-REF", f"session.{key}: no node named {source_id!r}")]
    if source.type != "agent" or source.config.get("harness") != node.config.get("harness"):
        return [
            where.problem(
                "E-SESSION-HARNESS",
                f"session.{key}: {source_id!r} must be an agent node on the same harness",
            )
        ]
    return []


def strict_schema_problem(schema: Any, path: str = "") -> str | None:
    """The first object in `schema` that OpenAI strict mode is reported to reject:
    one without `additionalProperties: false`, or with a property missing from
    `required` (spec §5.1.2, research §1.3)."""
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "object" or "properties" in schema:
        where = path or "the top level"
        if schema.get("additionalProperties") is not False:
            return f"{where} needs additionalProperties: false"
        missing = sorted(set(schema.get("properties") or {}) - set(schema.get("required") or []))
        if missing:
            return f"{where} does not list {', '.join(missing)} in required"
    children: list[tuple[str, Any]] = [
        (f"{path}.{key}" if path else key, sub)
        for key, sub in (schema.get("properties") or {}).items()
    ]
    if isinstance(schema.get("items"), dict):
        children.append((f"{path}[]", schema["items"]))
    for key in ("anyOf", "oneOf", "allOf"):
        children += [(path, sub) for sub in schema.get(key) or []]
    for name, definition in (schema.get("definitions") or schema.get("$defs") or {}).items():
        children.append((f"#{name}", definition))
    for child_path, child in children:
        problem = strict_schema_problem(child, child_path)
        if problem is not None:
            return problem
    return None


def subflow_checks(flow: Flow, _seen: frozenset[Path] = frozenset()) -> list[Problem]:
    """Child flows of subflow and map nodes load cleanly, and are passed only the
    inputs they declare, including every required one (spec §5.8, §5.9)."""
    problems: list[Problem] = []
    seen = _seen | {flow.path.resolve()}
    for node in flow.nodes.values():
        if node.type not in ("subflow", "map") or not isinstance(node.config.get("flow"), str):
            continue
        where = node.where("flow")
        path = (where.base_dir / node.config["flow"]).resolve()
        if path in seen:
            problems.append(where.problem("E-SCHEMA", f"{node.config['flow']} calls itself"))
            continue
        child, child_problems = load_flow(path)
        problems += [p for p in child_problems if p.is_error]
        if child is None:
            continue
        inputs = node.config.get("inputs")
        if isinstance(inputs, dict):
            origin = node.where("inputs")
            for name in inputs:
                if name not in child.inputs:
                    problems.append(
                        Origin(origin.doc, join_pointer(origin.pointer, name)).problem(
                            "E-UNKNOWN-REF",
                            f"{child.name} has no input named {name!r}",
                            key=True,
                        )
                    )
            for name, spec in child.inputs.items():
                if spec.get("required") and "default" not in spec and name not in inputs:
                    problems.append(
                        origin.problem("E-SCHEMA", f"{child.name} needs input {name!r}")
                    )
    return problems
