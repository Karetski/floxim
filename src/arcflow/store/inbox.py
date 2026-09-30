"""Requests from other processes to the runner (spec §7.4).

`arcflow respond`, `arcflow cancel` and the TUI never append events while a
runner is live: they drop a request file into `inbox/`, which the runner
consumes, turns into events, and deletes.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from arcflow.clock import Clock


def post(inbox: Path, request: dict[str, Any], clock: Clock) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().strftime("%Y%m%dT%H%M%S%f")
    name = f"{stamp}-{secrets.token_hex(4)}.json"
    temp = inbox / f".{name}.tmp"
    temp.write_text(json.dumps(request))
    os.replace(temp, inbox / name)
    return inbox / name


def pending(inbox: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Requests in arrival order. Unreadable files are skipped (and left for doctor)."""
    if not inbox.is_dir():
        return []
    requests = []
    for path in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            requests.append((path, data))
    return requests


def consume(path: Path) -> None:
    path.unlink(missing_ok=True)
