"""The text graph renderer."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from whisperwind import render
from whisperwind.flow import load_flow

FLOWS = Path(__file__).parent / "flows"
GOLDEN = Path(__file__).parent / "golden" / "graphs"
CORPUS = sorted(
    [*FLOWS.glob("valid/*.yaml"), *FLOWS.glob("warnings/*.yaml"), *FLOWS.glob("edit/*.yaml")]
)


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_given_corpus_flow_when_rendered_then_it_matches_its_golden_picture(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    import os

    flow, problems = load_flow(path)
    assert flow is not None, problems
    monkeypatch.setattr(render, "MAX_LAYOUT_S", 60.0)  # golden files never fall back for speed

    # When
    picture = render.render(flow)

    # Then
    golden = GOLDEN / f"{path.parent.name}-{path.stem}.txt"
    if os.environ.get("WHISPERWIND_UPDATE_GOLDEN"):
        golden.write_text(picture.text)
    assert picture.text == golden.read_text()
    assert set(picture.regions) >= set(flow.nodes)


def test_given_loop_when_rendered_then_the_back_edge_is_dashed_and_points_back() -> None:
    flow, _ = load_flow(FLOWS / "valid" / "implement-feature.yaml")
    assert flow is not None
    text = render.render(flow).text
    assert "◀┄" in text and "┆" in text


def test_given_status_marker_when_rendered_then_boxes_show_it() -> None:
    flow, _ = load_flow(FLOWS / "valid" / "implement-feature.yaml")
    assert flow is not None
    marks = {"plan": "✓", "implement": "▶"}
    text = render.render(
        flow, decorate=lambda node: f"{marks[node]} " if node in marks else ""
    ).text
    assert "│ ✓ ◆ plan │" in text and "▶ ◆ implement" in text


def _chain(count: int) -> str:
    nodes = "".join(
        f"  n{i}: {{type: set, vars: {{x: {i}}}, next: n{i + 1}}}\n" for i in range(count - 1)
    )
    return f"name: long\nnodes:\n{nodes}  n{count - 1}: {{type: set, vars: {{x: 0}}}}\n"


def test_given_more_than_sixty_nodes_when_rendered_then_it_falls_back_to_a_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / "long.yaml"
    path.write_text(_chain(61))
    flow, _ = load_flow(path)
    assert flow is not None
    picture = render.render(flow)
    assert picture.fallback
    assert picture.lines[:2] == ["= n0", "    → n1"]


def test_given_slow_layout_when_rendered_then_it_falls_back_to_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "short.yaml"
    path.write_text(_chain(5))
    flow, _ = load_flow(path)
    assert flow is not None
    real_monotonic = time.monotonic
    ticks = iter([0.0, 10.0, 20.0, 30.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, real_monotonic() + 100))
    assert render.render(flow).fallback


def test_given_small_flow_when_rendered_then_it_is_laid_out_not_listed(tmp_path: Path) -> None:
    path = tmp_path / "short.yaml"
    path.write_text(_chain(5))
    flow, _ = load_flow(path)
    assert flow is not None
    picture = render.render(flow)
    assert not picture.fallback and picture.lines[0].strip().startswith("┌")
