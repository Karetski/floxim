"""The TUI's screens."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.json import JSON
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Collapsible,
    DataTable,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from whisperwind import runinfo
from whisperwind.clock import Clock, parse_iso
from whisperwind.config import Config
from whisperwind.engine.budget import run_limits
from whisperwind.engine.human import choice_values
from whisperwind.engine.runner import reload_problem
from whisperwind.flow import Flow, load_flow
from whisperwind.store.events import read_log
from whisperwind.store.rundir import RunDir
from whisperwind.tui.actions import (
    AnswerModal,
    RunModal,
    cancel_run,
    clean,
    editor_command,
    interactive,
    resume_run,
    who,
)
from whisperwind.tui.common import (
    RUN_STATUS_ORDER,
    STATUS,
    FileWatcher,
    GraphView,
    NavFooter,
    local_time,
    local_timestamp,
    node_status,
    status_text,
)
from whisperwind.units import parse_duration
from whisperwind.validate import validate

REFRESH_S = 1.0
DETAIL_REFRESH_S = 0.5
FILE_POLL_S = 0.3
HIGHLIGHT_S = 2.0
BAR_CELLS = 8
LOG_TAIL = 4000
# The inspector's tabs, in order. A tab shows only when the selected visit has something
# for it; Flow shows while the flow file differs from the run's snapshot.
TABS = ("prompt", "output", "activity", "logs", "artifacts", "usage", "flow")


def _money(value: float | None) -> str:
    return "—" if not value else f"~${value:.2f}"


def _duration(item: dict[str, Any]) -> str:
    started = item.get("started_at") or item.get("created_at")
    if not started:
        return "—"
    end = parse_iso(item["finished_at"]) if item.get("finished_at") else Clock().now()
    seconds = int((end - parse_iso(started)).total_seconds())
    return (
        f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
        if seconds >= 3600
        else f"{seconds // 60}m{seconds % 60:02d}s"
    )


def _times(info: dict[str, Any]) -> str:
    """When a run started and finished, in local time, and how long it took.

    Only new events redraw the run's screen, so an active run shows no duration that
    would stall; the runs list has its live one."""
    started = info["started_at"] or info["created_at"]
    if not started:
        return ""
    if not info["finished_at"]:
        return f"started {local_timestamp(started)}"
    return (
        f"started {local_timestamp(started)}  ·  "
        f"finished {local_timestamp(info['finished_at'])}  ·  took {_duration(info)}"
    )


def _count(value: float) -> str:
    """A token count in a few characters: 950, 12.3k, 10M."""
    for size, unit in ((1e6, "M"), (1e3, "k")):
        if value >= size:
            return f"{value / size:.1f}".removesuffix(".0") + unit
    return f"{value:.0f}"


def _span(seconds: float) -> str:
    """A length of time in its two largest units: 32s, 5m12s, 1h30m, 8h."""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        minutes, rest = divmod(whole, 60)
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, rest = divmod(whole, 3600)
    return f"{hours}h{rest // 60:02d}m" if rest // 60 else f"{hours}h"


def _limits(totals: dict[str, Any], limits: dict[str, Any]) -> Text:
    """What a run has spent against each of its limits, on one line. A limit at least half
    used gets a bar; one that is `none` shows the spend alone."""
    usd, tokens = run_limits(limits)
    steps = limits.get("max_steps", 200)
    steps = None if steps == "none" else float(steps)
    duration = parse_duration(limits.get("max_duration", "8h"))
    spent = totals["usd_spent"], totals["tokens_spent"], totals["steps"], totals["active_s"]
    segments = [
        (spent[0], usd, f"${spent[0]:.2f}", f"${usd:g}" if usd else "", ""),
        (spent[1], tokens, _count(spent[1]), _count(tokens) if tokens else "", " tokens"),
        (spent[2], steps, str(spent[2]), f"{steps:g}" if steps else "", " steps"),
        (spent[3], duration, _span(spent[3]), _span(duration) if duration else "", ""),
    ]
    line = Text()
    for value, limit, shown, shown_limit, unit in segments:
        if line:
            line.append("  ·  ")
        line.append(f"{shown} / {shown_limit}{unit}" if limit else f"{shown}{unit}")
        used = value / limit if limit else 0.0
        if used >= 0.5:
            filled = min(round(used * BAR_CELLS), BAR_CELLS)
            line.append(" ")
            line.append("━" * filled, "yellow")
            line.append("━" * (BAR_CELLS - filled), "dim")
            line.append(f" {used:.0%}")
    return line


def _failure(info: dict[str, Any]) -> Text:
    """Why a run ended without succeeding: where, by whom, and the reason, or nothing."""
    failure = info["failure"] or {}
    if not failure:
        return Text("")
    node, message = failure.get("node"), str(failure.get("message") or "")
    if node:  # a node's error message starts with the node's name
        message = message.removeprefix(f"{node}: ")
    return Text.assemble(
        f"{STATUS.get(info['status'], ('?', ''))[0]} {info['status']}",
        f" at {node}" if node else "",
        f" by {failure['by']}" if failure.get("by") else "",
        f"  {failure['reason']}" if failure.get("reason") else "",
        f": {clean(message)}" if message else "",
    )


def _visit_ends(
    events: list[dict[str, Any]],
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    """Each finished visit's `visit_finished` event, and the route the run took after it:
    a route leaves the latest finished visit of the node it comes from."""
    finished: dict[tuple[str, int], dict[str, Any]] = {}
    routes: dict[tuple[str, int], dict[str, Any]] = {}
    latest: dict[str, tuple[str, int]] = {}
    for event in events:
        if event["type"] == "visit_finished":
            key = (event["node"], event["visit"])
            finished[key] = event
            latest[event["node"]] = key
        elif event["type"] == "route_taken" and event["data"].get("from") in latest:
            routes.setdefault(latest[event["data"]["from"]], event["data"])
    return finished, routes


def _route_text(outcome: str, route: dict[str, Any]) -> str:
    """Where the run went after a visit, and how when it wasn't the usual `next`."""
    via = route.get("via")
    if via == "resume":
        return f"resumed → {route['to']}"
    if via == "on_error":
        return f"on_error → {route['to']}"
    if outcome != "succeeded":  # `on_error: continue` routes a failure through `next`
        return f"continued → {route['to']}"
    return f"→ {route['to']}"


def _human_prompt(asked: dict[str, Any], result: dict[str, Any]) -> Text:
    """What a person was asked, what answer it takes, and, once given, the answer."""
    text = Text(clean(str(asked.get("message") or "")).strip())
    for line in asked.get("show") or []:
        text.append(f"\n  {clean(str(line))}", "dim")
    values = choice_values(asked.get("choices"))
    takes = " / ".join(values) if values else "an acknowledgement" if asked.get("ack") else "text"
    text.append(f"\n\ntakes    {clean(takes)}", "dim")
    if result:
        answer = result.get("choice") or result.get("text")
        answer = answer or ("acknowledged" if result.get("acknowledged") else "none")
        if result.get("timed_out"):
            answer = f"{answer} (timed out)"
        by = f"  by {result['responder']}" if result.get("responder") else ""
        via = f" via {result['via']}" if result.get("via") else ""
        text.append(f"\nanswer   {clean(str(answer))}{by}{via}")
    return text


def _output(result: dict[str, Any]) -> Text | None:
    """A finished visit's error and what it produced: structured output, an agent's
    text, the values a `set` node stored, a condition's branch or a shell's exit code."""
    parts: list[Text] = []
    error = result.get("error")
    if error:
        message = clean(str(error.get("message") or ""))
        parts.append(Text(f"{error.get('kind')}: {message}", style="red"))
    output = result.get("output")
    if isinstance(output, str):
        parts.append(Text(clean(output)))
    elif output is not None:
        parts.append(JSON.from_data(output).text)
    elif result.get("text"):
        parts.append(Text(clean(str(result["text"]))))
    elif result.get("values"):
        parts.append(JSON.from_data(result["values"]).text)
    elif result.get("branch"):
        parts.append(Text(f"→ {result['branch']}"))
    elif result.get("exit_code") is not None:
        parts.append(Text(f"exit_code {result['exit_code']}"))
    return Text("\n").join(parts) if parts else None


def _usage(result: dict[str, Any]) -> Text | None:
    """A visit's harness, session, turns, cost and tokens, in two aligned columns."""
    usage = result.get("usage") or {}
    rows = [
        ("harness", " ".join(str(result[k]) for k in ("harness", "model") if result.get(k))),
        ("session", result.get("session_id") or ""),
        ("turns", str(result["num_turns"]) if result.get("num_turns") is not None else ""),
        (
            "cost",
            f"${result['cost_usd']:.2f}" + (" (estimated)" if result.get("cost_estimated") else "")
            if result.get("cost_usd") is not None
            else "",
        ),
    ]
    for kind in ("input", "cached_input", "output", "reasoning", "total"):
        if usage.get(f"{kind}_tokens"):
            rows.append((f"{kind.replace('_', ' ')} tokens", f"{usage[f'{kind}_tokens']:,}"))
    rows = [(name, value) for name, value in rows if value]
    if not rows:
        return None
    width = max(len(name) for name, _ in rows)
    return Text("\n").join(
        Text.assemble((name.ljust(width + 2), "dim"), clean(str(value))) for name, value in rows
    )


def snapshot_of(run: RunDir) -> Path | None:
    relative = run.read_state().get("run", {}).get("snapshot") or run.meta().get("snapshot_flow")
    return run.path / str(relative) if relative else None


def run_flow_of(run: RunDir) -> Flow | None:
    snapshot = snapshot_of(run)
    if snapshot is None:
        return None
    flow, _ = load_flow(snapshot)
    return flow


class VisitItem(ListItem):
    """A timeline entry for one visit."""

    def __init__(self, visit: tuple[str, int], *children: Any) -> None:
        super().__init__(*children)
        self.visit = visit


class ChildItem(VisitItem):
    """A child run of a `map` or `subflow` visit, under that visit; choosing it opens it."""

    def __init__(self, visit: tuple[str, int], run_id: str, *children: Any) -> None:
        super().__init__(visit, *children)
        self.run_id = run_id


def pending_banner(config: Config) -> Text:
    """Every prompt waiting in the project, for the banner on list screens."""
    lines = []
    for item in runinfo.list_runs(config.runs_dir, Clock(), runinfo.RunFilter(active_only=True)):
        for node in item["pending"]:
            lines.append(f"? {item['run_id']} · {node} is waiting for an answer (a)")
    return Text("\n".join(lines), style="magenta bold")


class RunControl(Screen[None]):
    """Answer, cancel and resume, shared by the runs list and run detail."""

    config: Config

    def selected_run(self) -> RunDir | None:
        raise NotImplementedError

    def action_handoff(self) -> None:
        self.notify("open the run to hand over its session")

    def action_answer(self) -> None:
        run = self.selected_run()
        if run is None:
            return
        pending = run.read_state().get("pending_human") or {}
        if not pending:
            self.notify("nothing is waiting for an answer")
            return
        node, prompt = next(iter(pending.items()))
        if prompt.get("kind") == "handoff":
            self.action_handoff()
            return
        self.app.push_screen(AnswerModal(self.config, run, node, prompt))

    def action_cancel_run(self) -> None:
        run = self.selected_run()
        if run is not None:
            self.notify(cancel_run(self.config, run))

    def action_resume_run(self) -> None:
        run = self.selected_run()
        if run is not None:
            self.notify(resume_run(self.config, run))


class RunsScreen(RunControl):
    """Active and recent runs; pending prompts first."""

    BINDINGS = [
        Binding("enter", "open", "Open"),
        Binding("s", "cycle_filter", "Filter status"),
        Binding("a", "answer", "Answer"),
        Binding("c", "cancel_run", "Cancel"),
        Binding("u", "resume_run", "Resume"),
    ]
    FILTERS = (None, "waiting", "running", "failed", "succeeded")

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.status_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        yield Label("", id="runs-filter")
        # Status and run stay put when a narrow terminal scrolls the other columns.
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True, fixed_columns=2)
        yield NavFooter()

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        table.add_columns(
            "status",
            "run",
            "flow",
            "current",
            "started",
            "finished",
            "duration",
            "spend",
            "waiting on",
        )
        self.refresh_runs()
        self.set_interval(REFRESH_S, self.refresh_runs)

    def refresh_runs(self) -> None:
        runs = runinfo.list_runs(self.config.runs_dir, Clock(), runinfo.RunFilter())
        if self.status_filter:
            runs = [r for r in runs if r["status"] == self.status_filter]
        runs.sort(key=lambda r: RUN_STATUS_ORDER.get(r["status"], 9))
        table = self.query_one("#runs", DataTable)
        cursor = table.cursor_row
        table.clear()
        for item in runs:
            started = item["started_at"] or item["created_at"]
            table.add_row(
                status_text(item["status"]),
                item["run_id"],
                item["flow"] or "",
                item["current"] or "",
                local_time(started) if started else "",
                local_time(item["finished_at"]) if item["finished_at"] else "",
                _duration(item),
                _money(item["totals"]["usd_spent"]),
                ", ".join(item["pending"]),
                key=item["run_id"],
            )
        if runs:
            table.move_cursor(row=min(cursor, len(runs) - 1))
        label = f"filter: {self.status_filter or 'all'}  ·  {len(runs)} run(s)"
        self.query_one("#runs-filter", Label).update(label)
        self.query_one("#banner", Static).update(pending_banner(self.config))

    def selected_run(self) -> RunDir | None:
        table = self.query_one("#runs", DataTable)
        if not table.row_count:
            return None
        run_id = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return RunDir(self.config.runs_dir / run_id)

    def action_cycle_filter(self) -> None:
        index = self.FILTERS.index(self.status_filter)
        self.status_filter = self.FILTERS[(index + 1) % len(self.FILTERS)]
        self.refresh_runs()

    def action_open(self) -> None:
        table = self.query_one("#runs", DataTable)
        if table.row_count:
            run_id = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            self.app.push_screen(RunDetailScreen(self.config, run_id))

    @on(DataTable.RowSelected, "#runs")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(RunDetailScreen(self.config, str(event.row_key.value)))


class FlowsScreen(Screen[None]):
    """Flow files under flow_paths."""

    BINDINGS = [
        Binding("enter", "open", "Graph"),
        Binding("x", "run_flow", "Run"),
        Binding("v", "validate_flow", "Validate"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        yield DataTable(id="flows", cursor_type="row", zebra_stripes=True)
        yield NavFooter()

    def on_mount(self) -> None:
        table = self.query_one("#flows", DataTable)
        table.add_columns("", "flow", "file", "last run", "description")
        self.refresh_flows()
        self.set_interval(REFRESH_S * 5, self.refresh_flows)

    def refresh_flows(self) -> None:
        last: dict[str, str] = {}
        for item in runinfo.list_runs(self.config.runs_dir, Clock(), runinfo.RunFilter()):
            last.setdefault(str(item["flow"]), str(item["status"]))
        table = self.query_one("#flows", DataTable)
        table.clear()
        for item in runinfo.discover_flows(self.config):
            badge = Text("✓", style="green") if item["valid"] else Text("✗", style="red")
            description = (item["description"] or "").splitlines()[0] if item["description"] else ""
            self.query_one("#banner", Static).update(pending_banner(self.config))
            status = last.get(str(item["name"]))
            table.add_row(
                badge,
                item["name"] or "?",
                str(Path(item["file"]).relative_to(self.config.root))
                if Path(item["file"]).is_relative_to(self.config.root)
                else item["file"],
                status_text(status) if status else "",
                description,
                key=item["file"],
            )

    def selected_flow(self) -> Path | None:
        table = self.query_one("#flows", DataTable)
        if not table.row_count:
            return None
        return Path(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))

    def action_open(self) -> None:
        path = self.selected_flow()
        if path is not None:
            self.app.push_screen(FlowGraphScreen(path))

    def action_run_flow(self) -> None:
        path = self.selected_flow()
        if path is None:
            return

        def started(run_id: str | None) -> None:
            if run_id:
                self.app.push_screen(RunDetailScreen(self.config, run_id))

        self.app.push_screen(RunModal(self.config, path), started)

    def action_validate_flow(self) -> None:
        path = self.selected_flow()
        if path is None:
            return
        report = validate(path, config=self.config, workdir=self.config.root)
        if report.ok():
            self.notify(f"{path.name} is valid ({len(report.problems)} warning(s))")
        else:
            self.notify(report.errors[0].render(), severity="error")

    @on(DataTable.RowSelected, "#flows")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(FlowGraphScreen(Path(str(event.row_key.value))))


class RunDetailScreen(RunControl):
    """One run: live graph, visit timeline, inspector, gauges."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "answer", "Answer"),
        Binding("c", "cancel_run", "Cancel"),
        Binding("u", "resume_run", "Resume"),
        Binding("h", "handoff", "Handoff"),
        Binding("g", "open_in_harness", "Open session"),
        Binding("y", "copy_session", "Copy session ID"),
        Binding("o", "open_artifact", "Open artifact"),
        Binding("L", "reload_flow", "Resume with edited flow"),
    ]

    def __init__(self, config: Config, run_id: str) -> None:
        super().__init__()
        self.config = config
        self.run = RunDir(config.runs_dir / run_id)
        self.flow = run_flow_of(self.run)
        self.seen_events = -1
        self.selected: tuple[str, int] | None = None
        self.flow_diff = ""
        self.selected_child: str | None = None
        # Each child run's status as last shown: a child's progress adds no events here.
        self.child_status: dict[str, str] = {}
        # The inspector tab the person last picked, and the tab the screen is opening
        # itself (at first, the one the inspector starts on), whose activation is no pick.
        self.picked: str | None = None
        self.opened: str | None = "tab-prompt"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield Static("", id="failure")
        yield Static("", id="banner")
        with Collapsible(id="inputs"):
            yield Static("", id="inputs-list")
        yield Static("", id="limits")
        with Horizontal(id="body"):
            with VerticalScroll(id="graph-pane"):
                yield GraphView(id="graph")
            with Vertical(id="side"):
                yield ListView(id="timeline")
                with TabbedContent(id="inspector"):
                    for tab in TABS:
                        with (
                            TabPane(tab.capitalize(), id=f"tab-{tab}"),
                            VerticalScroll(id=f"scroll-{tab}"),
                        ):
                            yield Static("", id=f"inspect-{tab}")
        yield NavFooter()

    def on_mount(self) -> None:
        inputs = self.run.meta().get("inputs") or {}
        box = self.query_one("#inputs", Collapsible)
        box.display = bool(inputs)
        box.title = f"inputs ({len(inputs)})"
        listed = (
            f"{name} = {json.dumps(value, ensure_ascii=False)}" for name, value in inputs.items()
        )
        self.query_one("#inputs-list", Static).update(Text(clean("\n".join(listed))))
        self.refresh_detail()
        self.set_interval(DETAIL_REFRESH_S, self.refresh_detail)

    def selected_run(self) -> RunDir | None:
        return self.run

    def _flow_change(self) -> str:
        """A diff of the run's snapshot against the flow file as it is now, or ""."""
        import difflib

        current = Path(str(self.run.meta().get("flow_path")))
        snapshot = snapshot_of(self.run)
        if snapshot is None or not current.exists() or not snapshot.exists():
            return ""
        before, after = snapshot.read_text(), current.read_text()
        if before == after:
            return ""
        diff = difflib.unified_diff(
            before.splitlines(), after.splitlines(), "run snapshot", str(current), lineterm=""
        )
        return "\n".join(diff)

    def _reload_problem(self) -> str | None:
        """Why `resume --reload` cannot continue this run with the edited flow."""
        status = runinfo.display_status(self.run, self.run.read_state(), Clock())
        if status not in ("interrupted", "waiting"):
            return f"the run is {status}"
        flow, _ = load_flow(Path(str(self.run.meta().get("flow_path"))))
        if flow is None or self.flow is None:
            return "the edited flow does not load"
        return reload_problem(self.flow, flow, self.run.read_state().get("nodes") or {})

    def action_reload_flow(self) -> None:
        from whisperwind.engine.respond import spawn_detached

        if not self._flow_change():
            self.notify("the flow has not changed since the run started")
            return
        problem = self._reload_problem()
        if problem is not None:
            self.notify(f"cannot resume with the edited flow: {problem}", severity="error")
            return
        spawn_detached(self.run, self.config.root, ["--reload"])
        self.notify("resuming with the edited flow")

    def _selected_result(self) -> dict[str, Any]:
        if self.selected is None:
            return {}
        node, visit = self.selected
        visits = (self.run.read_state()["nodes"].get(node) or {}).get("visits", [])
        return next((v for v in visits if v.get("visit") == visit), {})

    def action_copy_session(self) -> None:
        session = self._selected_result().get("session_id")
        if session:
            self.app.copy_to_clipboard(str(session))
            self.notify(f"copied {session}")
        else:
            self.notify("the selected visit has no session")

    def _session_command(self) -> tuple[list[str], str] | None:
        from whisperwind.adapters import registry

        result = self._selected_result()
        session, harness = result.get("session_id"), result.get("harness")
        if not session or not harness:
            return None
        adapter = registry.load(str(harness), self.config.root)
        command = adapter.interactive_command(str(session), str(self.run.meta().get("workdir")))
        return (command, str(self.run.meta().get("workdir"))) if command else None

    def action_open_in_harness(self) -> None:
        found = self._session_command()
        if found is None:
            self.notify("the selected visit has no session to open")
            return
        with self.app.suspend():
            interactive(*found)

    def action_handoff(self) -> None:
        pending = self.run.read_state().get("pending_human") or {}
        node = next((n for n, p in pending.items() if p.get("kind") == "handoff"), None)
        if node is None:
            self.notify("no handoff is waiting")
            return
        prompt = pending[node]
        with self.app.suspend():
            code = interactive([str(part) for part in prompt.get("command") or []])
        from whisperwind.engine.human import Answer
        from whisperwind.engine.respond import respond

        answer = Answer(acknowledged=True, responder=who(), via="handoff", exit_code=code)
        respond(self.run, node, answer, clock=Clock(), project_root=self.config.root)
        self.notify("handed back; the run continues")

    def action_open_artifact(self) -> None:
        if self.selected is None:
            return
        node, visit = self.selected
        found = [a for a in runinfo.artifacts(self.run, node) if a["visit"] == visit]
        if not found:
            self.notify("the selected visit has no artifacts")
            return
        with self.app.suspend():
            interactive(editor_command(found[0]["path"]))

    def refresh_detail(self) -> None:
        events = read_log(self.run.events).events
        if len(events) == self.seen_events and not self._children_changed():
            # A running visit's logs grow without new events.
            if self.selected is not None and not self._selected_result():
                self._inspect()
            return
        self.seen_events = len(events)
        state = self.run.read_state()
        info = runinfo.detail(self.run, Clock())
        active = info["current"] and info["status"] in runinfo.ACTIVE
        self.query_one("#summary", Static).update(
            Text.assemble(
                status_text(info["status"]),
                f"  {info['run_id']}  ·  {info['flow']}",
                f"  ·  at {info['current']}" if active else "",
                "\n",
                _times(info),
            )
        )
        failure = self.query_one("#failure", Static)
        failure.update(_failure(info))
        failure.display = bool(info["failure"])
        banner = self.query_one("#banner", Static)
        pending = info["pending_human"]
        banner.update(
            Text(
                "\n".join(
                    f"? {node}: {clean(str(prompt.get('message', ''))).strip()}  (a to answer)"
                    for node, prompt in pending.items()
                ),
                style="magenta bold",
            )
            if pending
            else ""
        )
        changed = self._flow_change()
        if changed:
            lines = [str(banner.render())] if pending else []
            hint = (
                "L resumes with it"
                if self._reload_problem() is None
                else "it cannot replace this run's flow"
            )
            lines.append(f"flow changed since this run started; {hint}")
            banner.update(Text("\n".join(line for line in lines if line), style="magenta bold"))
        banner.display = bool(pending) or bool(changed)
        self.flow_diff = changed
        if self.flow is not None:
            self.query_one("#graph", GraphView).show(
                self.flow, node_status(state, info["status"]), state.get("visits")
            )
            self.query_one("#limits", Static).update(_limits(info["totals"], self.flow.limits))
        self._timeline(events, set(info["pending_human"]), info["status"])
        self._inspect()

    def _timeline(self, events: list[dict[str, Any]], waiting: set[str], status: str) -> None:
        """Every visit with its outcome and start time; once finished, how long it took,
        its cost and attempts, and where the run went next. An unfinished visit is waiting
        when its node has a pending prompt; otherwise it shares the run's status, such as
        interrupted."""
        timeline = self.query_one("#timeline", ListView)
        timeline.clear()
        started = [e for e in events if e["type"] == "visit_started"]
        finished, routes = _visit_ends(events)
        children: dict[tuple[str, int], list[tuple[str, int | None]]] = {}
        for e in events:
            if e["type"] == "child_run":
                children.setdefault((e["node"], e["visit"]), []).append(
                    (e["data"]["run_id"], e["data"].get("item_index"))
                )
        rows: list[tuple[tuple[str, int], str | None]] = []
        width = max((len(f"{e['node']} #{e['visit']}") for e in started), default=0)
        for event in started:
            key = (event["node"], event["visit"])
            done, route = finished.get(key), routes.get(key)
            outcome = (
                done["data"]["outcome"] if done else "waiting" if key[0] in waiting else status
            )
            marker, style = STATUS.get(outcome, ("?", ""))
            facts = []
            # Only new events redraw the timeline, so a running visit's duration would stall.
            if done:
                result = done["data"].get("result") or {}
                facts.append(
                    _span((parse_iso(done["ts"]) - parse_iso(event["ts"])).total_seconds())
                )
                if result.get("cost_usd"):
                    facts.append(f"${result['cost_usd']:.2f}")
                if (result.get("attempts") or 1) > 1:
                    facts.append(f"{result['attempts']} attempts")
            if route is not None:
                facts.append(_route_text(outcome, route))
                if outcome != "succeeded" and route.get("via") != "resume":
                    style = "yellow"  # a failure the flow handled
            label = Text.assemble(
                (marker, style),
                f" {f'{key[0]} #{key[1]}':<{width}}",
                (f"  {local_time(event['ts'])}", "dim"),
                *(("  " + fact, "dim") for fact in facts),
            )
            rows.append((key, None))
            timeline.append(VisitItem(key, Label(label)))
            for run_id, index in children.get(key, []):
                rows.append((key, run_id))
                timeline.append(ChildItem(key, run_id, Label(self._child_label(run_id, index))))
        if self.selected is None and started:
            self.selected = (started[-1]["node"], started[-1]["visit"])
        for row in ((self.selected, self.selected_child), (self.selected, None)):
            if row in rows:
                timeline.index = rows.index(row)
                break

    def _child_summary(self, run_id: str) -> dict[str, Any]:
        try:
            return runinfo.summary(RunDir(self.run.path.parent / run_id), Clock())
        except (OSError, ValueError, KeyError):
            return {}

    def _child_label(self, run_id: str, index: int | None) -> Text:
        """A child run under its visit: status, flow, item index, and how it stands."""
        info = self._child_summary(run_id)
        status = str(info.get("status") or "?")
        self.child_status[run_id] = status
        marker, style = STATUS.get(status, ("?", ""))
        facts = []
        if info.get("finished_at"):
            started = info["started_at"] or info["created_at"]
            facts.append(
                _span((parse_iso(info["finished_at"]) - parse_iso(started)).total_seconds())
            )
        elif info.get("current") and status in runinfo.ACTIVE:
            facts.append(f"at {info['current']}")
        if (info.get("totals") or {}).get("usd_spent"):
            facts.append(f"${info['totals']['usd_spent']:.2f}")
        return Text.assemble(
            "   ↳ ",
            (marker, style),
            f" {info.get('flow') or run_id}",
            f" [{index}]" if index is not None else "",
            *(("  " + fact, "dim") for fact in facts),
        )

    def _children_changed(self) -> bool:
        """Whether an active child run has moved on since the timeline last showed it."""
        return any(
            self._child_summary(run_id).get("status") != status
            for run_id, status in self.child_status.items()
            if status in runinfo.ACTIVE
        )

    @on(ListView.Highlighted, "#timeline")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            self.selected = getattr(event.item, "visit", None)
            self.selected_child = getattr(event.item, "run_id", None)
            self._inspect()

    @on(ListView.Selected, "#timeline")
    def _chosen(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ChildItem):
            self.app.push_screen(RunDetailScreen(self.config, event.item.run_id))

    @on(TabbedContent.TabActivated, "#inspector")
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == self.opened:
            self.opened = None
        else:
            self.picked = event.pane.id

    def _visit_tabs(self, node: str, visit: int) -> dict[str, Text]:
        """The inspector's content for one visit, by tab, for the tabs it has anything for."""
        result = self._selected_result()
        events = runinfo.events(self.run, node, visit)
        tabs: dict[str, Text] = {}
        prompt = runinfo.visit_files(self.run, node, visit, "prompt.md")
        asked = next((e["data"] for e in events if e["type"] == "human_waiting"), None)
        if prompt:
            tabs["prompt"] = Text(clean(prompt[0].read_text(errors="replace")))
        elif asked is not None:
            tabs["prompt"] = _human_prompt(asked, result)
        # A person's answer is shown with the prompt; only an error remains as output.
        output = _output({"error": result.get("error")} if asked is not None else result)
        if output is not None:
            tabs["output"] = output
        activity = [
            f"{e['data'].get('kind')}: {e['data'].get('summary')}"
            for e in events
            if e["type"] == "progress"
        ]
        if activity:
            tabs["activity"] = Text(clean("\n".join(activity)))
        logs = [
            f"--- {path.parent.name}/{path.name}\n{path.read_text(errors='replace')[-LOG_TAIL:]}"
            for name in ("stdout.log", "stderr.log")
            for path in runinfo.visit_files(self.run, node, visit, name)
        ]
        if logs:
            tabs["logs"] = Text(clean("\n".join(logs)))
        artifacts = [a["path"] for a in runinfo.artifacts(self.run, node) if a["visit"] == visit]
        if artifacts:
            tabs["artifacts"] = Text(clean("\n".join(artifacts)))
        usage = _usage(result)
        if usage is not None:
            tabs["usage"] = usage
        return tabs

    def _inspect(self) -> None:
        """Show the selected visit's tabs. The tab last picked stays open when the visit
        has it; otherwise the most useful one opens: a failed visit's logs, else output."""
        if self.selected is None:
            return
        contents = self._visit_tabs(*self.selected)
        if self.flow_diff:
            contents["flow"] = Text(self.flow_diff)
        if not contents:
            contents["output"] = Text("(nothing recorded yet)", style="dim")
        failed = self._selected_result().get("outcome") not in (None, "succeeded")
        best = "logs" if failed and "logs" in contents else "output"
        wanted = next(
            f"tab-{tab}"
            for tab in (self.picked and self.picked.removeprefix("tab-"), best, *TABS)
            if tab in contents
        )
        tabs = self.query_one("#inspector", TabbedContent)
        logs = self.query_one("#scroll-logs", VerticalScroll)
        following = logs.scroll_y >= logs.max_scroll_y
        for tab, content in contents.items():
            self.query_one(f"#inspect-{tab}", Static).update(content)
            tabs.show_tab(f"tab-{tab}")
        # Open the wanted tab before hiding the rest, so hiding never opens another one.
        if tabs.active != wanted:
            self.opened = wanted
            tabs.active = wanted
        for tab in TABS:
            if tab not in contents:
                tabs.hide_tab(f"tab-{tab}")
        if following:
            self.call_after_refresh(logs.scroll_end, animate=False)


class FlowGraphScreen(Screen[None]):
    """A flow's graph, live-synced with its file, and its editor."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("e", "edit_field", "Edit field"),
        Binding("p", "edit_prompt", "Prompt in $EDITOR"),
        Binding("n", "add_node", "Add"),
        Binding("d", "remove_node", "Remove"),
        Binding("R", "rename_node", "Rename"),
        Binding("k", "connect", "Connect"),
        Binding("x", "disconnect", "Disconnect"),
        Binding("o", "reorder", "Reorder cases"),
        Binding("t", "change_type", "Change type"),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.watcher = FileWatcher(path)
        self.flow: Flow | None = None
        self.previous: dict[str, Any] = {}
        self.node: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(str(self.path), id="flow-title")
        with Horizontal(id="body"):
            with VerticalScroll(id="graph-pane"):
                yield GraphView(id="graph")
            with Vertical(id="side"):
                yield ListView(id="node-list")
                with VerticalScroll(id="node-inspector"):
                    yield Static("", id="node-config")
        yield Static("", id="problems")
        yield NavFooter()

    def on_mount(self) -> None:
        self.poll()
        self.set_interval(FILE_POLL_S, self.poll)

    def poll(self) -> None:
        if not self.watcher.changed():
            return
        report = validate(self.path, implementation_gate=False)
        flow, errors = report.flow, report.errors
        panel = self.query_one("#problems", Static)
        graph = self.query_one("#graph", GraphView)
        if flow is None or errors:
            panel.update(Text("\n".join(p.render() for p in errors), style="red"))
            if self.flow is not None:
                graph.show(self.flow, dim=True)
            return
        panel.update("")
        changed = {
            node.id
            for node in flow.nodes.values()
            if self.previous and self.previous.get(node.id) != node.config
        }
        self.previous = {node.id: node.config for node in flow.nodes.values()}
        self.flow = flow
        graph.show(flow, highlight=changed)
        if changed:
            self.set_timer(HIGHLIGHT_S, lambda: graph.show(flow))
        self._list_nodes(flow)

    def _list_nodes(self, flow: Flow) -> None:
        listing = self.query_one("#node-list", ListView)
        listing.clear()
        for node in flow.nodes.values():
            listing.append(NodeItem(node.id, Label(f"{node.id}  ({node.type})")))
        if self.node not in flow.nodes:
            self.node = next(iter(flow.nodes), None)
        index = list(flow.nodes).index(self.node) if self.node else 0
        listing.index = index
        self._inspect()

    @on(ListView.Highlighted, "#node-list")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        # A rebuilt list still delivers highlights for the items it replaced.
        if isinstance(event.item, NodeItem) and self.flow and event.item.node in self.flow.nodes:
            self.node = event.item.node
            self._inspect()

    def _inspect(self) -> None:
        from whisperwind.tui.editor import yaml_text

        if self.flow is None or self.node not in self.flow.nodes:
            return
        node = self.flow.nodes[self.node]
        self.query_one("#node-config", Static).update(
            Text(yaml_text({"type": node.type, **node.config}))
        )

    def _edit(self, modal: Any, *, draft: bool = False) -> None:
        """Open an edit form; its edit applies to the file as it was when the form opened."""
        from whisperwind.tui.editor import file_sha

        based_on = file_sha(self.path)
        self.app.push_screen(modal, lambda op: self._apply(op, based_on, draft=draft))

    def _apply(
        self, operation: Any, based_on: str, *, draft: bool = False, allow_invalid: bool = False
    ) -> None:
        """Write one edit. When the file changed meanwhile, reload and ask;
        when a free-text edit would make the flow invalid, offer to keep it as a draft."""
        from whisperwind.tui.editor import ConfirmModal, apply, file_sha

        if operation is None:
            return
        failure = apply(self.path, operation, based_on=based_on, allow_invalid=allow_invalid)
        self.poll()
        if failure is None:
            return
        if failure.kind == "conflict":
            fresh = file_sha(self.path)

            def retry(yes: bool | None) -> None:
                if yes:
                    self._apply(operation, fresh, draft=draft)

            question = f"{failure.message}. Apply your edit to the new version?"
            self.app.push_screen(ConfirmModal(question, "Apply"), retry)
        elif failure.kind == "invalid" and draft:

            def keep(yes: bool | None) -> None:
                if yes:
                    self._apply(operation, based_on, allow_invalid=True)

            question = f"{failure.message}\n\nSave it anyway, as a draft to fix later?"
            self.app.push_screen(ConfirmModal(question, "Save draft"), keep)
        else:
            self.notify(failure.message, severity="error")

    def _current(self) -> Any:
        if self.flow is None or self.node is None or self.node not in self.flow.nodes:
            self.notify("select a node first")
            return None
        return self.flow.nodes[self.node]

    def action_edit_field(self) -> None:
        from whisperwind.tui.editor import FieldModal

        node = self._current()
        if node is not None:
            self._edit(FieldModal(node.id, node.type, node.config), draft=True)

    def action_edit_prompt(self) -> None:
        from whisperwind import edit
        from whisperwind.tui.editor import edit_in_editor

        node = self._current()
        if node is None:
            return
        key = next((k for k in ("prompt", "message", "run") if k in node.config), None)
        if key is None:
            self.notify(f"{node.id} has no prompt, message or run to edit")
            return
        from whisperwind.tui.editor import file_sha

        based_on = file_sha(self.path)
        with self.app.suspend():
            updated = edit_in_editor(str(node.config[key]))
        if updated is not None:
            operation = lambda ed: edit.set_field(ed, node.id, key, updated)  # noqa: E731
            self._apply(operation, based_on, draft=True)

    def action_add_node(self) -> None:
        from whisperwind.tui.editor import AddNodeModal

        self._edit(AddNodeModal(self.node))

    def action_remove_node(self) -> None:
        from whisperwind.tui.editor import RemoveNodeModal

        node = self._current()
        if node is not None:
            self._edit(RemoveNodeModal(node.id))

    def action_rename_node(self) -> None:
        from whisperwind.tui.editor import RenameModal

        node = self._current()
        if node is not None:
            self._edit(RenameModal(node.id))

    def _targets(self) -> list[str]:
        assert self.flow is not None
        return [*self.flow.nodes, "end", "fail"]

    def action_connect(self) -> None:
        from whisperwind.tui.editor import ConnectModal

        node = self._current()
        if node is not None:
            self._edit(ConnectModal(node.id, self._targets()))

    def action_disconnect(self) -> None:
        from whisperwind.tui.editor import ConnectModal

        node = self._current()
        if node is not None:
            self._edit(ConnectModal(node.id, self._targets(), disconnect=True))

    def action_reorder(self) -> None:
        from whisperwind.tui.editor import ReorderModal

        node = self._current()
        if node is None:
            return
        cases = node.config.get("next")
        if not isinstance(cases, list) or len(cases) < 2:
            self.notify(f"{node.id} has no cases to reorder")
            return
        self._edit(ReorderModal(node.id, len(cases)))

    def action_change_type(self) -> None:
        from whisperwind.tui.editor import ChangeTypeModal

        node = self._current()
        if node is not None:
            self._edit(ChangeTypeModal(node.id, node.type, set(node.config)))


class NodeItem(ListItem):
    """A node in the editor's node list."""

    def __init__(self, node: str, *children: Any) -> None:
        super().__init__(*children)
        self.node = node
