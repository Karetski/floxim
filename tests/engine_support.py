"""Running flows in tests: virtual time, a temporary project, normalized events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow.config import load_config
from arcflow.engine.runner import Runner, create_run
from arcflow.store.events import Event, read_log
from arcflow.store.rundir import RunDir
from arcflow.testing import VirtualClock, run_virtual


@dataclass
class Result:
    status: str
    run: RunDir
    state: dict[str, Any]
    events: list[Event]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def visited(self) -> list[str]:
        return [e["node"] for e in self.events if e["type"] == "visit_started"]

    def normalized(self, tmp_path: Path) -> list[Event]:
        text = json.dumps(self.events)
        text = text.replace(self.run.id, "<run>").replace(str(tmp_path.resolve()), "<tmp>")
        text = re.sub(r'"(pid)": \d+', r'"\1": 0', text)
        text = re.sub(r'"host": "[^"]*"', '"host": "<host>"', text)
        return list(json.loads(text))


def run_flow(
    tmp_path: Path,
    flow: str,
    inputs: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> Result:
    (tmp_path / ".arcflow").mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    flow_path = tmp_path / "flow.yaml"
    flow_path.write_text(flow)
    config, problems = load_config(tmp_path)
    assert not problems, problems
    clock = VirtualClock()
    run = create_run(flow_path, inputs or {}, config=config, clock=clock, workdir=tmp_path)
    runner = Runner(
        run, config, clock, heartbeat=False, environ=environ or {"PATH": "/usr/bin:/bin"}
    )
    outcome = run_virtual(runner.run())
    return Result(outcome.status, run, outcome.state, read_log(run.events).events)
