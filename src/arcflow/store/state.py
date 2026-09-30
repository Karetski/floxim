"""Run state derived from the event log (spec §7.3).

`reduce(events)` is a pure fold: the same events always give the same state, so
`state.json` is only a cache that can be rebuilt at any time.
"""

from __future__ import annotations

import copy
from typing import Any

from arcflow.clock import parse_iso
from arcflow.store.events import Event

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

State = dict[str, Any]


def initial_state() -> State:
    return {
        "status": None,
        "current": None,
        "nodes": {},
        "visits": {},
        "vars": {},
        "totals": {
            "usd_spent": 0.0,
            "tokens_spent": 0,
            "usd_left": None,
            "tokens_left": None,
            "steps": 0,
            "active_s": 0.0,
        },
        "pending_human": {},
        "last_seq": 0,
        "run": {},
        "in_progress": None,
        "workspaces": {},
        "outputs": None,
        "failure": None,
        "cancel_requested": None,
        "runner": None,
        "awaiting_route": None,  # a node whose visit finished but whose route is not recorded
        "active_since": None,
        "last_ts": None,
    }


def reduce(events: list[Event], state: State | None = None) -> State:
    state = initial_state() if state is None else copy.deepcopy(state)
    for event in events:
        apply(state, event)
    return state


def _elapsed(start: str, end: str) -> float:
    return max((parse_iso(end) - parse_iso(start)).total_seconds(), 0.0)


def _start_active(state: State, ts: str) -> None:
    if state["active_since"] is None:
        state["active_since"] = ts


def _stop_active(state: State, ts: str) -> None:
    if state["active_since"] is not None:
        state["totals"]["active_s"] += _elapsed(state["active_since"], ts)
        state["active_since"] = None


def active_seconds(state: State, now: str) -> float:
    """Active time so far, counting the open interval up to `now`."""
    total = float(state["totals"]["active_s"])
    if state["active_since"] is not None:
        total += _elapsed(state["active_since"], now)
    return total


def apply(state: State, event: Event) -> None:
    kind = event["type"]
    data = event.get("data") or {}
    ts = event["ts"]
    node = event.get("node")
    handler = _HANDLERS.get(kind)
    if handler is not None:
        handler(state, event, data, ts, node)
    state["last_seq"] = event["seq"]
    state["last_ts"] = ts


def _run_created(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["status"] = "pending"
    state["run"] = {**data, "created_at": ts}


def _run_started(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["status"] = "running"
    state["run"]["started_at"] = state["run"].get("started_at") or ts
    state["runner"] = {"pid": data.get("pid"), "host": data.get("host")}
    _start_active(state, ts)


def _runner_attached(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    if state["runner"] is not None and state["last_ts"] is not None:
        _stop_active(state, state["last_ts"])  # the previous runner died without a word
    state["runner"] = {"pid": data.get("pid"), "host": data.get("host")}
    if state["status"] in TERMINAL:
        return
    if state["status"] in ("pending", "running"):
        state["status"] = "running"
        _start_active(state, ts)


def _runner_detached(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    _stop_active(state, ts)
    state["runner"] = None


def _visit_started(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["visits"][node] = event["visit"]
    state["current"] = node
    state["in_progress"] = {
        "node": node,
        "visit": event["visit"],
        "type": data.get("type"),
        "started_at": ts,
        "attempt": 0,
        "session_id": None,
        "wake_at": data.get("wake_at"),
        "workspace": data.get("workspace"),
        "config_ref": data.get("config_ref"),
        "data": data,
        "permission_denials": [],
        "attempts": [],
    }


def _attempt_started(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    progress = state["in_progress"]
    if progress is not None:
        progress["attempt"] = event.get("attempt", data.get("attempt"))
        progress["attempts"].append(
            {"attempt": progress["attempt"], "started_at": ts, "session_id": None, **data}
        )


def _session_started(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    progress = state["in_progress"]
    if progress is not None:
        progress["session_id"] = data.get("session_id")
        if progress["attempts"]:
            progress["attempts"][-1]["session_id"] = data.get("session_id")


def _attempt_finished(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    progress = state["in_progress"]
    if progress is not None and progress["attempts"]:
        progress["attempts"][-1].update({"finished_at": ts, **data})


def _permission_denied(
    state: State, event: Event, data: dict[str, Any], ts: str, node: Any
) -> None:
    if state["in_progress"] is not None:
        state["in_progress"]["permission_denials"].append(data)


def _visit_finished(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    result = dict(data.get("result") or {})
    result.setdefault("outcome", data.get("outcome"))
    result.setdefault("visit", event.get("visit"))
    previous = state["nodes"].get(node) or {}
    history = [*previous.get("visits", []), result]
    state["nodes"][node] = {**result, "visits": history}
    progress = state["in_progress"]
    if progress is not None and progress.get("type") == "set" and result["outcome"] == "succeeded":
        state["vars"].update(result.get("values") or {})
    state["in_progress"] = None
    if result["outcome"] != "interrupted":
        state["totals"]["steps"] += 1
    state["awaiting_route"] = node


def _route_taken(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["current"] = data.get("to")
    state["awaiting_route"] = None


def _run_reopened(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["status"] = "running"
    state["failure"] = None
    state["finished_at"] = None
    state["cancel_requested"] = None
    if state["runner"] is not None:
        _start_active(state, ts)


def _budget_updated(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["totals"].update(
        {k: data[k] for k in ("usd_spent", "tokens_spent", "usd_left", "tokens_left") if k in data}
    )


def _workspace_created(
    state: State, event: Event, data: dict[str, Any], ts: str, node: Any
) -> None:
    state["workspaces"][data["name"]] = data


def _human_waiting(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["pending_human"][node] = {**data, "visit": event.get("visit"), "since": ts}


def _run_waiting(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["status"] = "waiting"
    _stop_active(state, ts)


def _human_responded(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    pending = state["pending_human"].pop(node, None)
    if state["in_progress"] is not None and pending is not None:
        state["in_progress"]["response"] = {**data, "responded_at": ts}
    if not state["pending_human"] and state["status"] == "waiting":
        state["status"] = "running"
        if state["runner"] is not None:
            _start_active(state, ts)


def _cancel_requested(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["cancel_requested"] = {**data, "at": ts}


def _flow_reloaded(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
    state["run"]["flow_sha256"] = data.get("new_sha256")
    state["run"]["snapshot"] = data.get("snapshot")


def _finished(status: str) -> Any:
    def handler(state: State, event: Event, data: dict[str, Any], ts: str, node: Any) -> None:
        state["status"] = status
        _stop_active(state, ts)
        state["runner"] = None
        state["pending_human"] = {}
        state["in_progress"] = None
        state["awaiting_route"] = None
        if status == "succeeded":
            state["outputs"] = data.get("outputs")
        else:
            state["failure"] = {
                k: data.get(k) for k in ("reason", "node", "message", "by") if k in data
            }
        state["finished_at"] = ts

    return handler


_HANDLERS = {
    "run_created": _run_created,
    "run_started": _run_started,
    "runner_attached": _runner_attached,
    "runner_takeover": _runner_attached,
    "runner_detached": _runner_detached,
    "flow_reloaded": _flow_reloaded,
    "visit_started": _visit_started,
    "workspace_created": _workspace_created,
    "attempt_started": _attempt_started,
    "session_started": _session_started,
    "permission_denied": _permission_denied,
    "attempt_finished": _attempt_finished,
    "visit_finished": _visit_finished,
    "route_taken": _route_taken,
    "budget_updated": _budget_updated,
    "human_waiting": _human_waiting,
    "run_waiting": _run_waiting,
    "human_responded": _human_responded,
    "cancel_requested": _cancel_requested,
    "run_reopened": _run_reopened,
    "run_succeeded": _finished("succeeded"),
    "run_failed": _finished("failed"),
    "run_cancelled": _finished("cancelled"),
}
