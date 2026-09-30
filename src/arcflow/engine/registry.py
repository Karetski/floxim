"""Executors by node type. Adding a node type means adding it here (brief principle 9)."""

from __future__ import annotations

from arcflow.engine.agent import AgentExecutor
from arcflow.engine.human import HumanExecutor
from arcflow.engine.nodes import ConditionExecutor, Executor, SetExecutor, SleepExecutor
from arcflow.engine.notify import NotifyExecutor
from arcflow.engine.python import PythonExecutor
from arcflow.engine.shell import ShellExecutor

EXECUTORS: dict[str, Executor] = {
    "condition": ConditionExecutor(),
    "sleep": SleepExecutor(),
    "set": SetExecutor(),
    "shell": ShellExecutor(),
    "python": PythonExecutor(),
    "agent": AgentExecutor(),
    "human": HumanExecutor(),
    "notify": NotifyExecutor(),
}
