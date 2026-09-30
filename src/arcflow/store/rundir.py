"""The run directory (spec §7.2)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcflow.store.events import read_log
from arcflow.store.state import State, reduce


@dataclass
class RunDir:
    path: Path

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def run_json(self) -> Path:
        return self.path / "run.json"

    @property
    def events(self) -> Path:
        return self.path / "events.jsonl"

    @property
    def state_json(self) -> Path:
        return self.path / "state.json"

    @property
    def lock(self) -> Path:
        return self.path / "lock"

    @property
    def inbox(self) -> Path:
        return self.path / "inbox"

    def snapshot_dir(self, generation: int = 0) -> Path:
        return self.path / ("snapshot" if generation == 0 else f"snapshot-{generation}")

    def visit_dir(self, node: str, visit: int) -> Path:
        return self.path / "nodes" / node / str(visit)

    def attempt_dir(self, node: str, visit: int, attempt: int) -> Path:
        return self.visit_dir(node, visit) / f"attempt-{attempt}"

    def artifacts_dir(self, node: str, visit: int) -> Path:
        return self.visit_dir(node, visit) / "artifacts"

    # -- creation ---------------------------------------------------------

    @classmethod
    def create(cls, runs_dir: Path, run_id: str, meta: dict[str, Any]) -> RunDir:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / run_id
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        run = cls(path)
        write_json_atomic(run.run_json, meta)
        run.events.touch()
        return run

    def write_snapshot(self, files: list[Path], flow_file: Path, generation: int = 0) -> Path:
        """Copy the flow and its files, keeping their relative layout. Returns the
        snapshot's copy of the flow file."""
        target = self.snapshot_dir(generation)
        root = Path(os.path.commonpath([str(p.parent) for p in files]))
        for source in files:
            dest = target / source.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        return target / flow_file.resolve().relative_to(root)

    # -- reading ------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        data = json.loads(self.run_json.read_text())
        assert isinstance(data, dict)
        return data

    def read_state(self) -> State:
        """The run's state: `state.json` when it is current, else rebuilt from events."""
        log = read_log(self.events)
        last_seq = log.events[-1]["seq"] if log.events else 0
        try:
            cached = json.loads(self.state_json.read_text())
            if isinstance(cached, dict) and cached.get("last_seq") == last_seq:
                return cached
        except (OSError, ValueError):
            pass
        return reduce(log.events)

    def write_state(self, state: State) -> None:
        write_json_atomic(self.state_json, state)


def write_json_atomic(path: Path, data: Any) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
