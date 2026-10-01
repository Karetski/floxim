"""Project root and configuration.

Configuration describes the environment a flow runs in, never what the flow
means. Files merge in precedence order: built-in defaults, user config,
project config, then an explicit `--config` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whisperwind.flow import merge
from whisperwind.problems import Problem
from whisperwind.schema import Bool, Field, Int, ListOf, MapOf, Num, Str, Struct
from whisperwind.yamlio import load_file

PROJECT_DIR = ".whisperwind"

# Shell commands that `W-NO-HUMAN-BEFORE-RISKY` looks for, as regexes.
DEFAULT_RISKY_COMMANDS = (
    r"\bgit\s+push\b",
    r"\bgh\s+pr\s+merge\b",
    r"\bkubectl\b",
    r"\bterraform\s+apply\b",
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r",
)

PRICE = Struct(
    {
        "input_per_mtok": Field(Num(minimum=0), required=True),
        "cached_input_per_mtok": Field(Num(minimum=0)),
        "output_per_mtok": Field(Num(minimum=0), required=True),
    }
)
CONFIG = Struct(
    {
        "flow_paths": Field(ListOf(Str())),
        "runs_dir": Field(Str()),
        "harnesses": Field(
            MapOf(
                Struct(
                    {
                        "command": Field(Str()),
                        "tested_versions": Field(Str()),
                        "grace": Field(Str()),
                    }
                )
            )
        ),
        "env_passthrough": Field(ListOf(Str())),
        "prices": Field(MapOf(MapOf(PRICE))),
        "on_wait": Field(Str()),
        "retention": Field(Struct({"keep_days": Field(Int(minimum=0))})),
        "redact": Field(ListOf(Str())),
        "redact_streams": Field(Bool()),
        "allow_full": Field(Bool()),
        "risky_commands": Field(ListOf(Str())),
    },
    doc="Whisperwind configuration (.whisperwind/config.yaml or the user config file).",
)

DEFAULTS: dict[str, Any] = {
    "flow_paths": ["flows"],
    "runs_dir": ".whisperwind/runs",
    "harnesses": {},
    "env_passthrough": [],
    "prices": {},
    "on_wait": None,
    "retention": {"keep_days": 30},
    "redact": [],
    "redact_streams": True,
    "allow_full": False,
    "risky_commands": list(DEFAULT_RISKY_COMMANDS),
}


@dataclass
class Config:
    root: Path
    values: dict[str, Any]
    files: list[Path] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def runs_dir(self) -> Path:
        return self.root / str(self.values["runs_dir"])

    @property
    def flow_paths(self) -> list[Path]:
        return [self.root / str(p) for p in self.values["flow_paths"]]


def find_project_root(start: Path) -> Path:
    """The nearest ancestor with `.whisperwind/`, else the git repository root, else `start`."""
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / PROJECT_DIR).is_dir():
            return directory
    git_root = find_git_root(start)
    return git_root if git_root is not None else Path(start)


def find_git_root(start: Path) -> Path | None:
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "whisperwind" / "config.yaml"


def load_config(root: Path, explicit: Path | None = None) -> tuple[Config, list[Problem]]:
    """Load and merge configuration files. Problems carry file positions; the
    returned config uses defaults for any file that failed to load."""
    values = dict(DEFAULTS)
    problems: list[Problem] = []
    files: list[Path] = []
    candidates = [user_config_path(), root / PROJECT_DIR / "config.yaml"]
    if explicit is not None:
        candidates.append(explicit)
    for path in candidates:
        if not path.is_file():
            if path == explicit:
                problems.append(Problem("E-FILE-NOT-FOUND", f"config not found: {path}", str(path)))
            continue
        doc, load_problems = load_file(path)
        problems += load_problems
        if doc is None:
            continue
        data = doc.data if doc.data is not None else {}
        findings = CONFIG.check(data, "")
        for code, message, pointer, is_key in findings:
            problems.append(doc.problem(code, message, pointer, key=is_key))
        if findings:
            continue
        values = merge(values, data)
        files.append(path)
    return Config(root, values, files), problems


def config_json_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Karetski/whisperwind/schemas/config-1.json",
        "title": "Whisperwind configuration",
        **CONFIG.json_schema(),
    }
