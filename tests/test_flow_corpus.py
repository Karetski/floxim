"""The flow corpus (spec §13): every invalid file reports exactly the errors it is
annotated with, and every valid file reports no errors.

Annotations are comment lines, anywhere in the file:

    # expect: E-UNKNOWN-TARGET @ 41:13    (code at line:column)
    # expect: W-UNREACHABLE               (code, any position)

For invalid files the set of errors must match the annotations exactly; for both
kinds, every annotated warning or info must be reported. Other warnings are
ignored, so later milestones can add lints without editing every file.
"""

import re
from collections import Counter
from pathlib import Path

import pytest

from arcflow.validate import validate

CORPUS = Path(__file__).parent / "flows"
SPEC = Path(__file__).parent.parent / "docs" / "spec.md"
EXPECT = re.compile(r"#\s*expect:\s*([EWI]-[A-Z0-9-]+)(?:\s*@\s*(\d+):(\d+))?")


def _expectations(path: Path) -> list[tuple[str, int | None, int | None]]:
    found = []
    for match in EXPECT.finditer(path.read_text()):
        code, line, col = match.groups()
        found.append((code, int(line) if line else None, int(col) if col else None))
    return found


def _matches(
    expected: tuple[str, int | None, int | None], problem_key: tuple[str, int, int]
) -> bool:
    code, line, col = expected
    return code == problem_key[0] and (line is None or (line, col) == problem_key[1:])


def _check(path: Path, *, invalid: bool) -> None:
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


@pytest.mark.parametrize("path", sorted((CORPUS / "invalid").glob("*.yaml")), ids=lambda p: p.name)
def test_given_invalid_corpus_file_when_validated_then_reports_its_annotated_errors(
    path: Path,
) -> None:
    _check(path, invalid=True)


@pytest.mark.parametrize("path", sorted((CORPUS / "valid").glob("*.yaml")), ids=lambda p: p.name)
def test_given_valid_corpus_file_when_validated_then_reports_no_errors(path: Path) -> None:
    _check(path, invalid=False)


def test_given_spec_appendix_a_when_compared_then_corpus_copies_are_identical() -> None:
    # Given
    spec = SPEC.read_text()
    appendix = spec[spec.index("## Appendix A. Example flows") : spec.index("## Appendix B.")]
    blocks = re.findall(r"```yaml\n(.*?)```", appendix, re.S)

    # When
    names = [re.search(r"^name: (\S+)", block, re.M) for block in blocks]

    # Then
    assert len(blocks) == 5
    for block, name in zip(blocks, names, strict=True):
        assert name is not None
        corpus_file = CORPUS / "valid" / f"{name.group(1)}.yaml"
        assert corpus_file.read_text() == block, f"{corpus_file.name} drifted from spec Appendix A"
