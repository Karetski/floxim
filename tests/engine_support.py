"""Running flows in tests: virtual time, a temporary project, normalized events."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.clock import Clock
from floxim.config import load_config
from floxim.engine.runner import Runner, create_run
from floxim.store.events import Event, read_log
from floxim.store.rundir import RunDir
from floxim.testing import VirtualClock, run_virtual


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

    def normalized(self, tmp_path: Path, *, real_time: bool = False) -> list[Event]:
        """Events with run-specific values replaced. With `real_time`, timestamps,
        durations and session IDs are replaced too."""
        text = json.dumps(self.events)
        text = text.replace(self.run.id, "<run>").replace(str(tmp_path.resolve()), "<tmp>")
        text = re.sub(r'"(pid)": \d+', r'"\1": 0', text)
        text = re.sub(r'"host": "[^"]*"', '"host": "<host>"', text)
        text = re.sub(r'"floxim_version": "[^"]*"', '"floxim_version": "<version>"', text)
        if real_time:
            text = re.sub(r'"\d{4}-\d\d-\d\dT[\d:.]+Z"', '"<time>"', text)
            text = re.sub(r'"(duration_s|active_s)": [\d.]+', r'"\1": 0', text)
            # Number session IDs in the order the run first reports them.
            sessions: dict[str, str] = {}
            for found in re.findall(r'"session_id": "([^"]+)"', text):
                sessions.setdefault(found, f"<session-{len(sessions) + 1}>")
            for found, label in sessions.items():
                text = text.replace(found, label)
        return list(json.loads(text))


def run_flow(
    tmp_path: Path,
    flow: str,
    inputs: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    *,
    virtual: bool = True,
    grace: float = 10.0,
    config_text: str | None = None,
    on_wait: str = "exit",
) -> Result:
    """Create and run a flow in a temporary project. `virtual` runs it on virtual
    time; flows with real child processes need real time (`virtual=False`)."""
    (tmp_path / ".floxim").mkdir(exist_ok=True)
    if config_text is not None:
        (tmp_path / ".floxim" / "config.yaml").write_text(config_text)
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    flow_path = tmp_path / "flow.yaml"
    flow_path.write_text(flow)
    config, problems = load_config(tmp_path)
    assert not problems, problems
    clock: Clock = VirtualClock() if virtual else Clock()
    run = create_run(flow_path, inputs or {}, config=config, clock=clock, workdir=tmp_path)
    runner = Runner(
        run,
        config,
        clock,
        heartbeat=False,
        environ=environ or {"PATH": "/usr/bin:/bin"},
        grace=grace,
        on_wait=on_wait,
    )
    outcome = run_virtual(runner.run()) if virtual else asyncio.run(runner.run())
    return Result(outcome.status, run, outcome.state, read_log(run.events).events)
