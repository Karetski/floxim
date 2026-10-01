"""The flow corpus: every invalid file reports exactly the errors it is
annotated with, every valid file reports no errors, and every file under
`warnings/` reports no errors and exactly its annotated warnings and infos.

Annotations are comment lines, anywhere in the file:

    # expect: E-UNKNOWN-TARGET @ 41:13    (code at line:column)
    # expect: W-UNREACHABLE               (code, any position)

Annotations can also live in a sidecar `<file>.expect`, for corpus files that
must stay byte-identical to their copies in `examples/`.

For invalid files the set of errors must match the annotations exactly; for all
kinds, every annotated warning or info must be reported. Outside `warnings/`,
other warnings are ignored, so later lints need no edits to unrelated files.
"""

import re
from collections import Counter
from pathlib import Path

import pytest

from whisperwind.validate import validate

CORPUS = Path(__file__).parent / "flows"
EXAMPLES = Path(__file__).parent.parent / "examples"
EXPECT = re.compile(r"#\s*expect:\s*([EWI]-[A-Z0-9-]+)(?:\s*@\s*(\d+):(\d+))?")


def _expectations(path: Path) -> list[tuple[str, int | None, int | None]]:
    found = []
    sidecar = path.with_name(path.name + ".expect")
    text = path.read_text() + (sidecar.read_text() if sidecar.exists() else "")
    for match in EXPECT.finditer(text):
        code, line, col = match.groups()
        found.append((code, int(line) if line else None, int(col) if col else None))
    return found


def _matches(
    expected: tuple[str, int | None, int | None], problem_key: tuple[str, int, int]
) -> bool:
    code, line, col = expected
    return code == problem_key[0] and (line is None or (line, col) == problem_key[1:])


def _check(path: Path, *, invalid: bool, exact_warnings: bool = False) -> None:
    report = validate(path, implementation_gate=False)
    actual = [(p.code, p.line or 0, p.column or 0) for p in report.problems]
    expected = _expectations(path)
    for exp in expected:
        assert any(_matches(exp, a) for a in actual), f"{path.name}: expected {exp}, got {actual}"
    errors = [a for a in actual if a[0].startswith("E-")]
    if invalid:
        expected_errors = [e for e in expected if e[0].startswith("E-")]
        assert expected_errors, f"{path.name}: an invalid corpus file needs # expect: lines"
        assert Counter(a[0] for a in errors) == Counter(e[0] for e in expected_errors), (
            f"{path.name}: errors {errors} do not match expectations {expected_errors}"
        )
    else:
        assert errors == [], f"{path.name}: unexpected errors {errors}"
    if exact_warnings:
        others = Counter(a[0] for a in actual if not a[0].startswith("E-"))
        assert others == Counter(e[0] for e in expected if not e[0].startswith("E-")), (
            f"{path.name}: warnings {actual} do not match expectations {expected}"
        )


@pytest.mark.parametrize("path", sorted((CORPUS / "invalid").glob("*.yaml")), ids=lambda p: p.name)
def test_given_invalid_corpus_file_when_validated_then_reports_its_annotated_errors(
    path: Path,
) -> None:
    _check(path, invalid=True)


@pytest.mark.parametrize("path", sorted((CORPUS / "valid").glob("*.yaml")), ids=lambda p: p.name)
def test_given_valid_corpus_file_when_validated_then_reports_no_errors(path: Path) -> None:
    _check(path, invalid=False)


@pytest.mark.parametrize("path", sorted((CORPUS / "warnings").glob("*.yaml")), ids=lambda p: p.name)
def test_given_lint_corpus_file_when_validated_then_reports_exactly_its_warnings(
    path: Path,
) -> None:
    _check(path, invalid=False, exact_warnings=True)


def test_given_examples_directory_when_compared_then_it_matches_the_corpus() -> None:
    # Given: examples/ is the user-facing copy of some valid corpus flows and their files
    shipped = sorted(p.relative_to(EXAMPLES) for p in EXAMPLES.rglob("*") if p.is_file())

    # Then
    assert shipped, "examples/ is empty"
    for relative in shipped:
        corpus_file = CORPUS / "valid" / relative
        assert (EXAMPLES / relative).read_bytes() == corpus_file.read_bytes(), (
            f"examples/{relative} drifted from the valid corpus"
        )
