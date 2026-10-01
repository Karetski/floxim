"""The user docs stay true: flows they show validate, and their links resolve."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from whisperwind.validate import validate

ROOT = Path(__file__).parent.parent
DOCS = [ROOT / "README.md"]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's anchor for a Markdown heading."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(m) for m in re.findall(r"^#+ (.+)$", path.read_text(), re.M)}


def _flows(path: Path) -> list[str]:
    blocks = re.findall(r"```yaml\n(.*?)```", path.read_text(), re.S)
    return [b for b in blocks if re.search(r"^name:", b, re.M) and re.search(r"^nodes:", b, re.M)]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_given_user_doc_when_its_flows_are_validated_then_they_have_no_errors(
    doc: Path, tmp_path: Path
) -> None:
    for index, text in enumerate(_flows(doc)):
        path = tmp_path / f"flow{index}.yaml"
        path.write_text(text)
        errors = validate(path, implementation_gate=False).errors
        assert errors == [], f"{doc.name} flow {index}: {[p.render() for p in errors]}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_given_user_doc_when_its_links_are_followed_then_they_resolve(doc: Path) -> None:
    for link in LINK.findall(doc.read_text()):
        if re.match(r"[a-z]+:", link):
            continue  # external
        target, _, anchor = link.partition("#")
        path = (doc.parent / target).resolve() if target else doc
        assert path.exists(), f"{doc.name}: {link} does not exist"
        if anchor:
            assert anchor in _anchors(path), f"{doc.name}: {link} names no heading"
