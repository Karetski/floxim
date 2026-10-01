"""Executors by node type. Adding a node type means adding it here."""

from __future__ import annotations

from whisperwind.engine.agent import AgentExecutor
from whisperwind.engine.handoff import HandoffExecutor
from whisperwind.engine.human import HumanExecutor
from whisperwind.engine.nodes import ConditionExecutor, Executor, SetExecutor, SleepExecutor
from whisperwind.engine.notify import NotifyExecutor
from whisperwind.engine.python import PythonExecutor
from whisperwind.engine.shell import ShellExecutor
from whisperwind.engine.subflow import MapExecutor, SubflowExecutor

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
