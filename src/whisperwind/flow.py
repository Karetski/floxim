"""Loading a flow file into its effective form, in the stages of `whisperwind validate`.

Stages, stopping after the first one that reports an error:

1. YAML syntax (anchors, aliases, merge keys and duplicate keys rejected).
2. Schema: types, unknown keys and required keys of the file as written, and
   the `include` fragments it names.
3. Identifiers and reserved words.
4. Templates: `extends` resolution and cycles, then the effective node of every
   node (defaults, templates and its own keys merged) checked
   against its type.
5. Referenced files exist, and JSON Schemas are valid.

Graph, expression and adapter checks run on the result (see `whisperwind.validate`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whisperwind import jsonschemas
from whisperwind.flowspec import (
    AT_MOST_ONE,
    DEFAULT_COMMON_KEYS,
    EXACTLY_ONE,
    FILE_KEYS,
    FLOW,
    FORMAT_VERSION,
    INCLUDE_FILE,
    REQUIRED_KEYS,
    RESERVED_NODE_IDS,
    ROUTING_KEYS,
    SCHEMA_KEYS,
    WORKSPACE_TYPES,
    node_fields,
)
from whisperwind.problems import Problem, join_pointer
from whisperwind.schema import IDENTIFIER, Finding, closest
from whisperwind.yamlio import Document, load_file

_IDENTIFIER_RE = re.compile(IDENTIFIER)


@dataclass(frozen=True)
class Origin:
    """Where a value was written: the document and the JSON pointer inside it."""

    doc: Document
    pointer: str

    @property
    def base_dir(self) -> Path:
        return Path(self.doc.file).parent

    def problem(self, code: str, message: str, *, key: bool = False) -> Problem:
        return self.doc.problem(code, message, self.pointer, key=key)


@dataclass
class Node:
    """A node after defaults and templates are applied."""

    id: str
    type: str
    config: dict[str, Any]
    origins: dict[str, Origin]  # top-level key → where its value came from
    origin: Origin  # the node's own entry under `nodes`

    def where(self, key: str) -> Origin:
        return self.origins.get(key, self.origin)


@dataclass
class Flow:
    path: Path
    doc: Document
    name: str
    description: str | None
    inputs: dict[str, dict[str, Any]]
    defaults: dict[str, Any]
    limits: dict[str, Any]
    start: str
    nodes: dict[str, Node]
    outputs: dict[str, Any]
    on_wait: str | None
    includes: list[Document] = field(default_factory=list)
    # Loaded JSON Schemas by (file, pointer) of the key that names them.
    schemas: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def schema_for(self, node: Node, key: str) -> dict[str, Any] | None:
        origin = node.where(key)
        return self.schemas.get((origin.doc.file, origin.pointer))

    def files(self, _seen: set[Path] | None = None) -> list[Path]:
        """The flow file and every file it references, for run snapshots,
        including the files of subflows it calls."""
        seen = _seen if _seen is not None else set()
        seen.add(self.path.resolve())
        paths = [self.path, *(Path(d.file) for d in self.includes)]
        for node in self.nodes.values():
            child = node.config.get("flow") if node.type in ("subflow", "map") else None
            if not isinstance(child, str):
                continue
            child_path = (node.where("flow").base_dir / child).resolve()
            if child_path in seen:
                continue
            child_flow, _ = load_flow(child_path)
            if child_flow is not None:
                paths += child_flow.files(seen)
        for node in self.nodes.values():
            for key in FILE_KEYS.get(node.type, ()):
                value = node.config.get(key)
                if isinstance(value, str):
                    paths.append(node.where(key).base_dir / value)
        for spec in self.inputs.values():
            if isinstance(spec.get("schema"), str):
                paths.append(self.base_dir / spec["schema"])
        return list(dict.fromkeys(p.resolve() for p in paths))


@dataclass
class _Template:
    name: str
    body: dict[str, Any]
    origin: Origin


def load_flow(path: Path) -> tuple[Flow | None, list[Problem]]:
    """Load and check a flow file through stages 1–5. Returns the flow only if
    no stage reported an error; warnings are returned either way."""
    loader = _Loader(path)
    flow = loader.run()
    return flow, loader.problems


class _Loader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.problems: list[Problem] = []
        self.includes: list[Document] = []

    def _failed(self) -> bool:
        return any(p.is_error for p in self.problems)

    def _add(self, doc: Document, findings: list[Finding]) -> None:
        for code, message, pointer, is_key in findings:
            self.problems.append(doc.problem(code, message, pointer, key=is_key))

    def run(self) -> Flow | None:
        # Stage 1: YAML.
        doc, problems = load_file(self.path)
        self.problems += problems
        if doc is None:
            return None
        data = doc.data
        if not isinstance(data, dict):
            self.problems.append(doc.problem("E-SCHEMA", "a flow file must be a mapping", ""))
            return None

        # Stage 2: schema.
        version = data.get("whisperwind", FORMAT_VERSION)
        if isinstance(version, int) and not isinstance(version, bool) and version > FORMAT_VERSION:
            self.problems.append(
                doc.problem(
                    "E-VERSION",
                    f"format version {version} is newer than this Whisperwind supports "
                    f"({FORMAT_VERSION}); upgrade Whisperwind",
                    "/whisperwind",
                )
            )
            return None
        self._add(doc, FLOW.check(data, ""))
        if self._failed():
            return None
        templates = self._load_templates(doc)
        if self._failed():
            return None

        # Stage 3: identifiers.
        self._check_identifiers(doc, templates)
        if self._failed():
            return None

        # Stage 4: templates and effective nodes.
        nodes = self._effective_nodes(doc, templates)
        if self._failed():
            return None

        flow = Flow(
            path=self.path,
            doc=doc,
            name=data["name"],
            description=data.get("description"),
            inputs=data.get("inputs") or {},
            defaults=data.get("defaults") or {},
            limits=data.get("limits") or {},
            start=data.get("start") or next(iter(data["nodes"]), ""),
            nodes=nodes,
            outputs=data.get("outputs") or {},
            on_wait=data.get("on_wait"),
            includes=self.includes,
        )

        # Stage 5: referenced files and JSON Schemas.
        self._check_files(flow)
        if self._failed():
            return None
        return flow

    # -- stage 2 -----------------------------------------------------------

    def _load_templates(self, doc: Document) -> dict[str, _Template]:
        templates = {
            name: _Template(name, body, Origin(doc, join_pointer("/templates", name)))
            for name, body in (doc.data.get("templates") or {}).items()
        }
        for index, rel in enumerate(doc.data.get("include") or []):
            pointer = join_pointer("/include", index)
            inc_path = self.path.parent / rel
            if not inc_path.is_file():
                self.problems.append(
                    doc.problem("E-FILE-NOT-FOUND", f"included file not found: {rel}", pointer)
                )
                continue
            inc, problems = load_file(inc_path)
            self.problems += problems
            if inc is None:
                continue
            self.includes.append(inc)
            if not isinstance(inc.data, dict):
                self.problems.append(
                    inc.problem("E-SCHEMA", "an included file must be a mapping", "")
                )
                continue
            findings = INCLUDE_FILE.check(inc.data, "")
            self._add(inc, findings)
            if findings:
                continue
            for name, body in inc.data["templates"].items():
                if name in templates:
                    self.problems.append(
                        templates[name].origin.problem(
                            "E-DUPLICATE-KEY",
                            f"template {name!r} is also defined in included file {rel}",
                            key=True,
                        )
                    )
                    continue
                templates[name] = _Template(
                    name, body, Origin(inc, join_pointer("/templates", name))
                )
        return templates

    # -- stage 3 -----------------------------------------------------------

    def _check_identifiers(self, doc: Document, templates: dict[str, _Template]) -> None:
        def check(origin: Origin, name: str, what: str, reserved: bool = False) -> None:
            if not _IDENTIFIER_RE.match(name):
                self.problems.append(
                    origin.problem(
                        "E-BAD-ID",
                        f"{what} {name!r} must match {IDENTIFIER} "
                        "(lowercase letter, then lowercase letters, digits or _)",
                        key=True,
                    )
                )
            elif reserved and name in RESERVED_NODE_IDS:
                self.problems.append(
                    origin.problem(
                        "E-RESERVED-ID",
                        f"{name!r} is a reserved word and cannot name a node",
                        key=True,
                    )
                )

        data = doc.data
        for node_id, body in data["nodes"].items():
            node_origin = Origin(doc, join_pointer("/nodes", node_id))
            check(node_origin, node_id, "node ID", reserved=True)
            for var in (body.get("vars") or {}) if isinstance(body.get("vars"), dict) else {}:
                check(
                    Origin(doc, join_pointer(node_origin.pointer + "/vars", var)), var, "variable"
                )
        for template in templates.values():
            check(template.origin, template.name, "template name")
        for section, what in (("inputs", "input name"), ("outputs", "output name")):
            for name in data.get(section) or {}:
                check(Origin(doc, join_pointer(f"/{section}", name)), name, what)

    # -- stage 4 -----------------------------------------------------------

    def _chain(
        self, start: str | None, origin: Origin, templates: dict[str, _Template]
    ) -> list[_Template] | None:
        """Templates named by `extends`, most specific first; None on an error."""
        chain: list[_Template] = []
        seen: set[str] = set()
        name = start
        where = origin
        while name is not None:
            if name in seen:
                cycle = " → ".join([t.name for t in chain] + [name])
                self.problems.append(
                    where.problem("E-TEMPLATE-CYCLE", f"templates extend each other: {cycle}")
                )
                return None
            template = templates.get(name)
            if template is None:
                hint = closest(name, templates)
                self.problems.append(
                    where.problem(
                        "E-UNKNOWN-TEMPLATE",
                        f"no template named {name!r}"
                        + (f" (did you mean {hint!r}?)" if hint else ""),
                    )
                )
                return None
            seen.add(name)
            chain.append(template)
            where = Origin(template.origin.doc, template.origin.pointer + "/extends")
            extends = template.body.get("extends")
            name = extends if isinstance(extends, str) else None
        return chain

    def _effective_nodes(self, doc: Document, templates: dict[str, _Template]) -> dict[str, Node]:
        for template in templates.values():
            for key in ROUTING_KEYS:
                if key in template.body:
                    self.problems.append(
                        Origin(
                            template.origin.doc, join_pointer(template.origin.pointer, key)
                        ).problem(
                            "E-TEMPLATE-ROUTING",
                            f"templates cannot contain {key!r}; routing lives on the node",
                            key=True,
                        )
                    )
        defaults = doc.data.get("defaults") or {}
        nodes: dict[str, Node] = {}
        for node_id, body in doc.data["nodes"].items():
            node_origin = Origin(doc, join_pointer("/nodes", node_id))
            extends = body.get("extends")
            chain = self._chain(
                extends if isinstance(extends, str) else None,
                Origin(doc, node_origin.pointer + "/extends"),
                templates,
            )
            if chain is None:
                continue
            layers = [(t.body, t.origin) for t in reversed(chain)] + [(body, node_origin)]
            node_type = next(
                (b["type"] for b, _ in reversed(layers) if isinstance(b.get("type"), str)), None
            )
            if node_type is None:
                self.problems.append(
                    node_origin.problem("E-SCHEMA", f"node {node_id!r} has no type", key=True)
                )
                continue
            config: dict[str, Any] = {}
            origins: dict[str, Origin] = {}
            common = {
                k: defaults[k]
                for k in DEFAULT_COMMON_KEYS
                if k in defaults and (k not in ("workspace", "env") or node_type in WORKSPACE_TYPES)
            }
            _merge_layer(config, origins, common, Origin(doc, "/defaults"))
            if isinstance(defaults.get(node_type), dict):
                _merge_layer(
                    config, origins, defaults[node_type], Origin(doc, f"/defaults/{node_type}")
                )
            for layer, origin in layers:
                _merge_layer(config, origins, layer, origin)
            config.pop("extends", None)
            origins.pop("extends", None)
            node = Node(node_id, node_type, config, origins, node_origin)
            self._check_node(node)
            nodes[node_id] = node
        return nodes

    def _check_node(self, node: Node) -> None:
        fields = node_fields(node.type)
        for key, value in node.config.items():
            origin = node.where(key)
            spec = fields.get(key)
            if spec is None:
                if not key.startswith("x-"):
                    hint = closest(key, fields)
                    self.problems.append(
                        origin.problem(
                            "E-UNKNOWN-KEY",
                            f"{node.type} nodes have no key {key!r}"
                            + (f" (did you mean {hint!r}?)" if hint else ""),
                            key=True,
                        )
                    )
                continue
            self._add(origin.doc, spec.type.check(value, origin.pointer))
        for key in REQUIRED_KEYS.get(node.type, ()):
            if key not in node.config:
                self.problems.append(
                    node.origin.problem(
                        "E-SCHEMA", f"{node.type} node {node.id!r} needs {key!r}", key=True
                    )
                )
        for group in EXACTLY_ONE.get(node.type, ()):
            present = [k for k in group if k in node.config]
            if not present:
                self.problems.append(
                    node.origin.problem(
                        "E-SCHEMA",
                        f"{node.type} node {node.id!r} needs one of: {', '.join(group)}",
                        key=True,
                    )
                )
        for group in (*EXACTLY_ONE.get(node.type, ()), *AT_MOST_ONE.get(node.type, ())):
            present = [k for k in group if k in node.config]
            if len(present) > 1:
                self.problems.append(
                    node.where(present[-1]).problem(
                        "E-MUTUALLY-EXCLUSIVE",
                        f"{' and '.join(present)} cannot both be set",
                        key=True,
                    )
                )
        if node.type == "human":
            self._check_human(node)
        if node.type == "map" and node.config.get("concurrency", 1) != 1:
            self.problems.append(
                node.where("concurrency").problem(
                    "E-SCHEMA", "concurrency is reserved and must be 1 in this version"
                )
            )

    def _check_human(self, node: Node) -> None:
        config = node.config
        if config.get("ack") and ("choices" in config or config.get("input") == "text"):
            self.problems.append(
                node.where("ack").problem(
                    "E-MUTUALLY-EXCLUSIVE",
                    "ack cannot be combined with choices or input: text",
                    key=True,
                )
            )
        if "choices" not in config and not config.get("ack") and config.get("input") == "none":
            self.problems.append(
                node.where("input").problem(
                    "E-SCHEMA", "without choices or ack, a human node needs input: text"
                )
            )
        if "default" in config:
            values = [c["value"] if isinstance(c, dict) else c for c in config.get("choices", [])]
            if config["default"] not in values:
                self.problems.append(
                    node.where("default").problem(
                        "E-SCHEMA", f"default {config['default']!r} is not one of the choices"
                    )
                )

    # -- stage 5 -----------------------------------------------------------

    def _check_files(self, flow: Flow) -> None:
        for node in flow.nodes.values():
            for key in FILE_KEYS.get(node.type, ()):
                value = node.config.get(key)
                origin = node.where(key)
                if key in SCHEMA_KEYS and isinstance(value, dict):
                    self._check_schema(flow, value, origin, origin.pointer)
                elif isinstance(value, str):
                    self._check_file(flow, origin.base_dir / value, origin, key in SCHEMA_KEYS)
        for name, spec in flow.inputs.items():
            schema = spec.get("schema")
            origin = Origin(flow.doc, join_pointer(join_pointer("/inputs", name), "schema"))
            if isinstance(schema, dict):
                self._check_schema(flow, schema, origin, origin.pointer)
            elif isinstance(schema, str):
                self._check_file(flow, flow.base_dir / schema, origin, True)

    def _check_file(self, flow: Flow, path: Path, origin: Origin, is_schema: bool) -> None:
        if not path.is_file():
            self.problems.append(origin.problem("E-FILE-NOT-FOUND", f"file not found: {path}"))
            return
        if not is_schema:
            return
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.problems.append(
                origin.problem("E-BAD-JSON-SCHEMA", f"{path} is not valid JSON: {exc}")
            )
            return
        self._check_schema(flow, schema, origin, origin.pointer)

    def _check_schema(self, flow: Flow, schema: Any, origin: Origin, pointer: str) -> None:
        error = jsonschemas.schema_error(schema)
        if error is not None:
            self.problems.append(origin.problem("E-BAD-JSON-SCHEMA", error))
            return
        flow.schemas[(origin.doc.file, pointer)] = schema


def _merge_layer(
    config: dict[str, Any], origins: dict[str, Origin], layer: dict[str, Any], origin: Origin
) -> None:
    for key, value in layer.items():
        if key.startswith("x-"):
            continue
        where = Origin(origin.doc, join_pointer(origin.pointer, key))
        if value is None:
            config.pop(key, None)
            origins.pop(key, None)
        elif isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = merge(config[key], value)
            origins[key] = where
        else:
            config[key] = value
            origins[key] = where


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """The template merge rule: mappings merge recursively, anything else replaces,
    and `null` removes the inherited key."""
    out = dict(base)
    for key, value in override.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out
