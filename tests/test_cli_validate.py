"""`floxim validate` and `floxim schema flow` on the command line (spec §9.1, §9.4)."""

import json
from pathlib import Path

import pytest

from floxim import cli

INVALID = Path(__file__).parent / "flows" / "invalid"


def test_given_invalid_flow_when_validated_then_exits_3_and_prints_positions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When
    code = cli.main(["validate", str(INVALID / "unknown-template.yaml")])

    # Then
    assert code == 3
    out = capsys.readouterr().out
    assert "unknown-template.yaml:6:14 error E-UNKNOWN-TEMPLATE" in out
    assert "nodes.review.extends" in out
    assert "did you mean 'reviewer'?" in out


def test_given_invalid_flow_when_validated_with_json_then_prints_problem_objects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When
    code = cli.main(["validate", "--json", str(INVALID / "unknown-template.yaml")])

    # Then
    assert code == 3
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["code"] == "E-INVALID-FLOW"
    problem = document["error"]["details"]["problems"][0]
    assert problem == {
        "code": "E-UNKNOWN-TEMPLATE",
        "severity": "error",
        "message": "no template named 'reviwer' (did you mean 'reviewer'?)",
        "file": str(INVALID / "unknown-template.yaml"),
        "line": 6,
        "column": 14,
        "pointer": "/nodes/review/extends",
        "hint": None,
    }


def test_given_missing_file_when_validated_then_reports_file_not_found(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # When
    code = cli.main(["validate", "--json", str(tmp_path / "nope.yaml")])

    # Then
    assert code == 3
    problems = json.loads(capsys.readouterr().out)["error"]["details"]["problems"]
    assert [p["code"] for p in problems] == ["E-FILE-NOT-FOUND"]


def test_when_flow_schema_requested_then_prints_a_json_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When
    code = cli.main(["schema", "flow"])

    # Then
    assert code == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert "nodes" in schema["required"]
