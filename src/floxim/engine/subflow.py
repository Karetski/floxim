"""`subflow` and `map`: child runs of another flow file (spec §5.8, §5.9).

A child run has its own run directory, linked to the parent by `parent` in its
run.json and by `child_run` in the parent's events. It runs in the parent's
process, with limits capped by what the parent has left. While the child waits
for a person the parent waits too: the parent records a `human_waiting` of
kind "child", so `floxim respond <parent>` can forward the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floxim.engine.human import WaitReleased
from floxim.engine.inputs import InputError
from floxim.engine.nodes import AttemptResult, VisitContext
from floxim.store.events import Event
from floxim.store.rundir import RunDir
from floxim.store.state import TERMINAL
from floxim.templates import render_value

MAX_DEPTH = 8
DEFAULT_MAX_ITEMS = 100


class ChildProblem(Exception):
    """The child run could not be created; the visit fails."""


@dataclass
class ChildResult:
    run_id: str
    status: str
    outputs: Any
    failure: dict[str, Any] | None

    def summary(self, index: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "outputs": self.outputs,
        }
        if index is not None:
            data = {"index": index, **data}
        return data


class ChildDriver:
    """Creates or reattaches one child run and runs it to an end or a wait."""

    def __init__(self, ctx: VisitContext) -> None:
        self.ctx = ctx
        self.child_id: str | None = None

    async def run(
        self, flow_path: Path, inputs: dict[str, Any], existing: str | None, item_index: int | None
    ) -> ChildResult:
        from floxim.engine.runner import (
            Detached,
            FlowInvalid,
            Runner,
            crash_hook_from_env,
            create_run,
        )

        ctx = self.ctx
        parent = ctx.runner
        if existing is not None:
            child = RunDir(parent.config.runs_dir / existing)
            state = child.read_state()
            if state["status"] in TERMINAL:
                return _result(child, state)
        else:
            depth = int(parent.meta.get("depth") or 0) + 1
            if depth > MAX_DEPTH:
                raise ChildProblem(f"subflows nest deeper than {MAX_DEPTH} levels")
            usd, tokens = parent.run_limits()
            totals = parent.state["totals"]
            cap = {
                "usd": None if usd is None else max(usd - totals["usd_spent"], 0.0),
                "tokens": None if tokens is None else max(tokens - totals["tokens_spent"], 0),
                "max_duration": parent.remaining_active_seconds(),
            }
            try:
                child = create_run(
                    flow_path,
                    inputs,
                    config=parent.config,
                    clock=parent.clock,
                    workdir=parent.workdir,
                    parent=parent.meta["id"],
                    limits_cap=cap,
                    depth=depth,
                )
            except FlowInvalid as exc:
                first = exc.report.errors[0].render() if exc.report.errors else "invalid"
                raise ChildProblem(f"child flow is invalid: {first}") from None
            except InputError as exc:
                raise ChildProblem(f"child inputs: {exc}") from None
            data: dict[str, Any] = {"run_id": child.id}
            if item_index is not None:
                data["item_index"] = item_index
            parent.emit("child_run", data, node=ctx.node.id, visit=ctx.visit)
        self.child_id = child.id
        runner = Runner(
            child,
            parent.config,
            parent.clock,
            on_event=self._forward,
            environ=parent.environ,
            heartbeat=parent.heartbeat_enabled,
            grace=parent.grace,
            on_wait=parent.on_wait,
            # Tests crash inside child runs with FLOXIM_TEST_CRASH_CHILD_AT (spec §13).
            crash_hook=crash_hook_from_env(
                {"FLOXIM_TEST_CRASH_AT": os.environ.get("FLOXIM_TEST_CRASH_CHILD_AT", "")}
            ),
        )
        task = asyncio.ensure_future(runner.run())
        stopper: asyncio.Future[Any] = asyncio.ensure_future(ctx.stop.event.wait())
        timeout = parent.timeout_for(ctx.node, ctx.config)
        try:
            done, _ = await asyncio.wait(
                {task, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                reason = ctx.stop.reason if stopper in done else "timeout"
                if reason == "shutdown":
                    runner.request_shutdown(urgent=ctx.stop.urgent.is_set())
                else:
                    cancel = parent.cancel or {}
                    runner.request_cancel(cancel.get("by", "floxim"), cancel.get("reason", reason))
                ctx.scratch["stop_reason"] = reason
            outcome = await task
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
        if outcome.status == "waiting":
            raise WaitReleased
        if outcome.status == "detached":
            raise Detached
        result = _result(child, outcome.state)
        self._add_spend(outcome.state)
        return result

    def _add_spend(self, state: dict[str, Any]) -> None:
        """Count the child's spend in the parent's budget."""
        parent = self.ctx.runner
        spent = state["totals"]
        if not (spent.get("usd_spent") or spent.get("tokens_spent")):
            return
        usd, tokens = parent.run_limits()
        totals = parent.state["totals"]
        usd_spent = round(totals["usd_spent"] + float(spent.get("usd_spent") or 0), 6)
        tokens_spent = int(totals["tokens_spent"] + int(spent.get("tokens_spent") or 0))
        parent.emit(
            "budget_updated",
            {
                "usd_spent": usd_spent,
                "tokens_spent": tokens_spent,
                "usd_left": None if usd is None else round(usd - usd_spent, 6),
                "tokens_left": None if tokens is None else int(tokens - tokens_spent),
            },
            node=self.ctx.node.id,
            visit=self.ctx.visit,
        )

    def _forward(self, event: Event) -> None:
        """Mirror a child's waits and answers in the parent (spec §5.8)."""
        ctx = self.ctx
        parent = ctx.runner
        node = ctx.node.id
        kind = event["type"]
        if kind == "human_waiting" and node not in parent.state["pending_human"]:
            data = event.get("data") or {}
            parent.emit(
                "human_waiting",
                {
                    **data,
                    "kind": "child",
                    "child_run": self.child_id,
                    "child_node": event.get("node"),
                },
                node=node,
                visit=ctx.visit,
            )
            parent.emit("run_waiting", {"nodes": sorted(parent.state["pending_human"])})
            parent.run_dir.write_state(parent.state)
        elif kind == "human_responded" and node in parent.state["pending_human"]:
            data = event.get("data") or {}
            parent.emit("human_responded", {**data, "via": "forwarded"}, node=node)


def _result(child: RunDir, state: dict[str, Any]) -> ChildResult:
    return ChildResult(child.id, str(state["status"]), state.get("outputs"), state.get("failure"))


def _child_ids(ctx: VisitContext) -> dict[int | None, str]:
    progress = ctx.runner.state.get("in_progress") or {}
    return {c.get("item_index"): c["run_id"] for c in progress.get("children") or []}


def _outcome_for_unfinished(ctx: VisitContext, result: ChildResult) -> AttemptResult:
    reason = ctx.scratch.get("stop_reason")
    fields = result.summary()
    if reason == "timeout":
        return AttemptResult(
            "timed_out", fields, {"kind": "timeout", "message": "the child run timed out"}
        )
    if result.status == "cancelled":
        return AttemptResult("cancelled", fields, None)
    failure = result.failure or {}
    message = f"child run {result.run_id} {result.status}: {failure.get('message') or ''}".strip()
    return AttemptResult("failed", fields, {"kind": "child_failed", "message": message})


class SubflowExecutor:
    default_on_resume = "restart"
    handles_timeout = True
    continues_waiting = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        path = ctx.node.where("flow").base_dir / str(ctx.config["flow"])
        inputs = ctx.config.get("inputs") or {}
        if not isinstance(inputs, dict):
            return AttemptResult.failed("expression_error", "inputs must be a mapping")
        try:
            result = await ChildDriver(ctx).run(path, inputs, _child_ids(ctx).get(None), None)
        except ChildProblem as exc:
            return AttemptResult.failed("child_failed", str(exc))
        if result.status != "succeeded":
            return _outcome_for_unfinished(ctx, result)
        fields = {**result.summary(), "output": result.outputs}
        return AttemptResult("succeeded", fields)


class MapExecutor:
    default_on_resume = "restart"
    handles_timeout = True
    continues_waiting = True

    def prepare(self, ctx: VisitContext) -> dict[str, Any]:
        return {}

    async def run(self, ctx: VisitContext) -> AttemptResult:
        config = ctx.config
        items = config.get("items")
        if not isinstance(items, list):
            return AttemptResult.failed("expression_error", "items must evaluate to a list")
        max_items = int(config.get("max_items", DEFAULT_MAX_ITEMS))
        if len(items) > max_items:
            return AttemptResult.failed(
                "limit", f"{len(items)} items is more than max_items ({max_items})"
            )
        path = ctx.node.where("flow").base_dir / str(config["flow"])
        existing = _child_ids(ctx)
        on_item_error = config.get("on_item_error", "fail")
        driver = ChildDriver(ctx)
        results: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            namespace = {**ctx.namespace, "item": item, "index": index}
            inputs = render_value(
                ctx.node.config.get("inputs") or {}, namespace, ctx.runner.clock.now
            )
            if not isinstance(inputs, dict):
                return AttemptResult.failed("expression_error", "inputs must be a mapping")
            try:
                result = await driver.run(path, inputs, existing.get(index), index)
            except ChildProblem as exc:
                return AttemptResult.failed("child_failed", f"item {index}: {exc}")
            results.append(result.summary(index))
            if result.status != "succeeded" and (
                on_item_error == "fail" or result.status == "cancelled" or ctx.stop.reason
            ):
                unfinished = _outcome_for_unfinished(ctx, result)
                unfinished.fields = _map_fields(results)
                return unfinished
        return AttemptResult("succeeded", _map_fields(results))


def _map_fields(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "results": results,
        "succeeded": sum(1 for r in results if r["status"] == "succeeded"),
        "failed": sum(1 for r in results if r["status"] != "succeeded"),
    }
