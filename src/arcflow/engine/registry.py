"""Executors by node type. Adding a node type means adding it here (brief principle 9)."""

from __future__ import annotations

from arcflow.engine.nodes import ConditionExecutor, Executor, SetExecutor, SleepExecutor
from arcflow.engine.shell import ShellExecutor

EXECUTORS: dict[str, Executor] = {
    "condition": ConditionExecutor(),
    "sleep": SleepExecutor(),
    "set": SetExecutor(),
    "shell": ShellExecutor(),
}
