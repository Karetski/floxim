"""Executors by node type. Adding a node type means adding it here."""

from __future__ import annotations

from floxim.engine.agent import AgentExecutor
from floxim.engine.handoff import HandoffExecutor
from floxim.engine.human import HumanExecutor
from floxim.engine.nodes import ConditionExecutor, Executor, SetExecutor, SleepExecutor
from floxim.engine.notify import NotifyExecutor
from floxim.engine.python import PythonExecutor
from floxim.engine.shell import ShellExecutor
from floxim.engine.subflow import MapExecutor, SubflowExecutor

EXECUTORS: dict[str, Executor] = {
    "condition": ConditionExecutor(),
    "sleep": SleepExecutor(),
    "set": SetExecutor(),
    "shell": ShellExecutor(),
    "python": PythonExecutor(),
    "agent": AgentExecutor(),
    "human": HumanExecutor(),
    "notify": NotifyExecutor(),
    "subflow": SubflowExecutor(),
    "map": MapExecutor(),
    "handoff": HandoffExecutor(),
}
