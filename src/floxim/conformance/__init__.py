"""The adapter conformance kit, `floxim adapter test` (spec §8.8).

Offline (default), it replays recorded streams and compares the normalized
results with each fixture set's `expected.yaml`: built-in adapters parse the
streams directly; command adapters run their real process path against a stub
executable that replays the stream. Live (`--live`, opt-in, spends money), it
runs a few small prompts through a real harness with a spend cap.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from floxim.adapters import AgentRequest, AgentResult, SessionSpec, registry
from floxim.adapters.process import ProcessAdapter, parse_lines

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPLAY = Path(__file__).parent / "replay.py"
SESSION = "11111111-2222-4333-8444-555555555555"


@dataclass
class CaseResult:
    case: str
    passed: bool
    problems: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"case": self.case, "passed": self.passed, "problems": self.problems}


def fixture_dirs(name: str, root: Path | None) -> list[Path]:
    """Fixture sets for an adapter: the project's own for command adapters,
    the packaged ones (one per tested harness version) for built-ins."""
    if root is not None:
        from floxim.adapters.command import find

        path = find(root, name)
        if path is not None:
            own = path.parent / name / "fixtures"
            return [own] if own.is_dir() else []
    return sorted(p for p in FIXTURES_DIR.glob(f"{name}-*") if p.is_dir())


def _request(attempt_dir: Path, schema: Any = None) -> AgentRequest:
    return AgentRequest(
        run_id="conformance",
        node_id="case",
        visit=1,
        attempt=1,
        prompt="Conformance test prompt.",
        instructions=None,
        cwd=str(attempt_dir),
        add_dirs=[],
        model=None,
        effort=None,
        permissions="edit",
        allow_tools=[],
        deny_tools=[],
        output_schema=schema,
        session=SessionSpec("new", None, SESSION),
        max_turns=None,
        budget_usd=None,
        budget_tokens=None,
        bare=False,
        env={"PATH": os.environ.get("PATH", "")},
        options={},
        attempt_dir=str(attempt_dir),
    )


def compare(result: AgentResult, expected: dict[str, Any]) -> list[str]:
    """Differences between a normalized result and its expectation."""
    problems = []

    def check(what: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            problems.append(f"{what}: expected {wanted!r}, got {actual!r}")

    if "outcome" in expected:
        check("outcome", result.outcome, expected["outcome"])
    if "error_kind" in expected:
        check("error kind", result.error.kind if result.error else None, expected["error_kind"])
    for key in ("session_id", "text", "output"):
        if key in expected:
            check(key, getattr(result, key), expected[key])
    if "cost_usd" in expected:
        actual = result.cost_usd
        if actual is None or abs(actual - float(expected["cost_usd"])) > 1e-9:
            problems.append(f"cost_usd: expected {expected['cost_usd']!r}, got {actual!r}")
    for key, wanted in (expected.get("usage") or {}).items():
        check(f"usage.{key}", getattr(result.usage, key), wanted)
    if "permission_denials" in expected:
        check(
            "permission denials",
            [d.tool for d in result.permission_denials],
            expected["permission_denials"],
        )
    return problems


def run_offline(name: str, root: Path | None) -> list[CaseResult]:
    adapter = registry.load(name, root)
    if not isinstance(adapter, ProcessAdapter) and name != "fake":
        raise ValueError(f"{name}: offline conformance needs a process-based adapter")
    results: list[CaseResult] = []
    dirs = fixture_dirs(name, root)
    if not dirs:
        return [CaseResult("fixtures", False, [f"no fixtures found for {name}"])]
    command_adapter = registry.source(name, root) not in ("built-in",)
    for directory in dirs:
        expected = YAML(typ="safe", pure=True).load((directory / "expected.yaml").read_text())
        for case, wanted in (expected.get("cases") or {}).items():
            label = f"{directory.name}/{case}"
            stream = directory / f"{case}.jsonl"
            if not stream.exists():
                results.append(CaseResult(label, False, [f"missing {stream.name}"]))
                continue
            with tempfile.TemporaryDirectory() as tmp:
                req = _request(Path(tmp), wanted.get("schema"))
                if command_adapter:
                    result = asyncio.run(_replay(adapter, req, stream))  # type: ignore[arg-type]
                else:
                    assert isinstance(adapter, ProcessAdapter)
                    parser = adapter.parser(req)
                    parse_lines(parser, stream.read_text().splitlines())
                    result = parser.finish(req, 0, "")
            problems = compare(result, wanted)
            results.append(CaseResult(label, not problems, problems))
    return results


async def _replay(adapter: ProcessAdapter, req: AgentRequest, stream: Path) -> AgentResult:
    """Run the adapter's real process path with its program replaced by a stub
    that replays `stream`."""
    original = adapter.command

    def command(request: AgentRequest) -> list[str]:
        argv = original(request)
        return [sys.executable, str(REPLAY), str(stream), "--", *argv[1:]]

    adapter.command = command  # type: ignore[method-assign, assignment]
    try:
        return await adapter.run(req, lambda event: None)
    finally:
        adapter.command = original  # type: ignore[method-assign]


LIVE_FLOW = """name: conformance-live
defaults:
  agent:
    harness: {name}
    timeout: 3m
    budget: {{usd: 0.05, tokens: 100000}}
nodes:
  first:
    type: agent
    permissions: read-only
    prompt: Reply with the word ready.
    output_schema:
      type: object
      additionalProperties: false
      required: [word]
      properties: {{word: {{type: string}}}}
    next: second
  second:
    type: agent
    permissions: read-only
    session: {{resume: first}}
    prompt: Which word did you reply with? Answer in one word.
    next: slow
  slow:
    type: agent
    timeout: 5s
    on_error: continue
    prompt: Count slowly from 1 to 500, one number per line.
"""


def run_live(name: str, root: Path | None, environ: dict[str, str]) -> list[CaseResult]:
    """A few real calls with a spend cap: session IDs, resume, schema output and
    stopping within the grace period."""
    from floxim.clock import Clock
    from floxim.config import load_config
    from floxim.engine.runner import Runner, create_run

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / ".floxim").mkdir()
        if root is not None and (root / ".floxim" / "harnesses").is_dir():
            import shutil

            shutil.copytree(root / ".floxim" / "harnesses", project / ".floxim" / "harnesses")
        flow = project / "flow.yaml"
        flow.write_text(LIVE_FLOW.format(name=name))
        config, _ = load_config(project)
        run = create_run(flow, {}, config=config, clock=Clock(), workdir=project)
        outcome = asyncio.run(Runner(run, config, Clock(), environ=environ, heartbeat=False).run())
        nodes = outcome.state["nodes"]
    first, second, slow = (
        nodes.get("first") or {},
        nodes.get("second") or {},
        nodes.get("slow") or {},
    )
    return [
        _live_case("session id", bool(first.get("session_id")), f"got {first.get('session_id')!r}"),
        _live_case(
            "schema output",
            isinstance(first.get("output"), dict) and "word" in first["output"],
            f"got {first.get('output')!r}",
        ),
        _live_case(
            "resume",
            second.get("session_id") == first.get("session_id")
            and second.get("outcome") == "succeeded",
            f"first {first.get('session_id')!r}, second {second.get('session_id')!r}",
        ),
        _live_case(
            "stop within grace", slow.get("outcome") == "timed_out", f"got {slow.get('outcome')!r}"
        ),
    ]


def _live_case(case: str, ok: bool, detail: str) -> CaseResult:
    return CaseResult(f"live/{case}", ok, [] if ok else [detail])
