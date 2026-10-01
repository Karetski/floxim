"""Loading a flow into effective nodes: defaults, templates, includes."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import fastjsonschema
import pytest

from whisperwind.flow import load_flow
from whisperwind.flowspec import flow_json_schema
from whisperwind.yamlio import load_file

VALID = Path(__file__).parent / "flows" / "valid"
INVALID = Path(__file__).parent / "flows" / "invalid"
WriteFlow = Callable[[dict[str, str]], Path]


@pytest.mark.parametrize("path", sorted(VALID.glob("*.yaml")), ids=lambda p: p.name)
def test_given_appendix_a_flow_when_loaded_then_effective_nodes_match_golden(
    path: Path, golden: Any
) -> None:
    # When
    flow, problems = load_flow(path)

    # Then
    assert flow is not None, problems
    effective = {
        node_id: {"type": node.type, "config": node.config} for node_id, node in flow.nodes.items()
    }
    golden.check(f"effective/{path.stem}.json", {"start": flow.start, "nodes": effective})


def test_given_defaults_and_templates_when_loaded_then_most_specific_value_wins(
    write_flow: WriteFlow,
) -> None:
    # Given
    path = write_flow(
        {
            "flow.yaml": """
name: layers
defaults:
  timeout: 1m
  max_visits: 3
  agent:
    harness: claude
    timeout: 2m
    harness_options: {a: 1, b: 1}
templates:
  base:
    type: agent
    timeout: 3m
    harness_options: {b: 2, c: 2}
  special:
    extends: base
    permissions: read-only
nodes:
  n:
    extends: special
    prompt: Hi.
    harness_options: {c: 3}
    max_visits: null
"""
        }
    )

    # When
    flow, problems = load_flow(path)

    # Then
    assert flow is not None, problems
    config = flow.nodes["n"].config
    assert config["harness"] == "claude"
    assert config["timeout"] == "3m"
    assert config["permissions"] == "read-only"
    assert config["harness_options"] == {"a": 1, "b": 2, "c": 3}
    assert "max_visits" not in config  # null removes the inherited value


def test_given_workspace_default_when_node_type_has_no_workspace_then_it_is_not_applied(
    write_flow: WriteFlow,
) -> None:
    # Given
    path = write_flow(
        {
            "flow.yaml": """
name: ws
defaults:
  workspace: worktree
nodes:
  check: {type: condition, next: [{to: end}]}
  build: {type: shell, run: make}
"""
        }
    )

    # When
    flow, problems = load_flow(path)

    # Then
    assert flow is not None, problems
    assert "workspace" not in flow.nodes["check"].config
    assert flow.nodes["build"].config["workspace"] == "worktree"


def test_given_no_start_when_loaded_then_the_first_node_is_the_start(write_flow: WriteFlow) -> None:
    # Given
    path = write_flow(
        {
            "flow.yaml": (
                "name: s\nnodes:\n"
                "  second: {type: sleep, duration: 1s}\n"
                "  z: {type: sleep, duration: 1s}\n"
            )
        }
    )

    # When
    flow, _ = load_flow(path)

    # Then
    assert flow is not None
    assert flow.start == "second"


def test_given_included_template_when_loaded_then_its_files_resolve_next_to_the_fragment(
    write_flow: WriteFlow,
) -> None:
    # Given
    path = write_flow(
        {
            "flows/flow.yaml": (
                "name: inc\ninclude: [../shared/t.yaml]\nnodes:\n  a: {extends: t}\n"
            ),
            "shared/t.yaml": "templates:\n  t: {type: agent, harness: fake, prompt_file: p.md}\n",
            "shared/p.md": "Hello",
        }
    )

    # When
    flow, problems = load_flow(path)

    # Then
    assert flow is not None, problems
    assert any(p.name == "p.md" for p in flow.files())


def _plain(path: Path) -> Any:
    doc, _ = load_file(path)
    assert doc is not None
    return doc.data


@pytest.mark.parametrize("path", sorted(VALID.glob("*.yaml")), ids=lambda p: p.name)
def test_given_published_schema_when_valid_flow_checked_then_it_is_accepted(path: Path) -> None:
    # Given
    validate = fastjsonschema.compile(flow_json_schema())

    # When / Then
    validate(_plain(path))


@pytest.mark.parametrize(
    "name",
    ["unknown-keys", "wrong-types", "missing-name", "wrong-key-for-type", "bad-flow-name"],
)
def test_given_published_schema_when_invalid_flow_checked_then_it_is_rejected(name: str) -> None:
    # Given
    validate = fastjsonschema.compile(flow_json_schema())

    # When / Then
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        validate(_plain(INVALID / f"{name}.yaml"))


def test_given_a_schema_when_checked_and_used_then_it_is_never_modified() -> None:
    # Given
    import copy

    from whisperwind import jsonschemas

    schema = {"type": "object", "properties": {"a": {"enum": [1]}, "b": {"default": 5}}}
    output = {"a": 1}
    original_schema, original_output = copy.deepcopy(schema), copy.deepcopy(output)

    # When
    assert jsonschemas.schema_error(schema) is None
    assert jsonschemas.validation_error(jsonschemas.compile_schema(schema), output) is None

    # Then
    assert schema == original_schema
    assert output == original_output
