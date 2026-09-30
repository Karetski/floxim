"""The TUI's screens (spec §10.1)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

from arcflow import runinfo
from arcflow.clock import Clock, parse_iso
from arcflow.config import Config
from arcflow.engine.budget import run_limits
from arcflow.flow import Flow, load_flow
from arcflow.store.events import read_log
from arcflow.store.rundir import RunDir
from arcflow.tui.common import RUN_STATUS_ORDER, FileWatcher, GraphView, node_status, status_text
from arcflow.units import parse_duration
from arcflow.validate import validate

REFRESH_S = 1.0
DETAIL_REFRESH_S = 0.5
FILE_POLL_S = 0.3
HIGHLIGHT_S = 2.0


def _money(value: float | None) -> str:
    return "—" if not value else f"~${value:.2f}"


def _duration(item: dict[str, Any]) -> str:
    started = item.get("started_at") or item.get("created_at")
    if not started:
        return "—"
    end = (
        parse_iso(item["finished_at"])
        if item.get("finished_at")
        else datetime.datetime.now(datetime.timezone.utc)
    )
    seconds = int((end - parse_iso(started)).total_seconds())
    return (
        f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
        if seconds >= 3600
        else f"{seconds // 60}m{seconds % 60:02d}s"
    )


def run_flow_of(run: RunDir) -> Flow | None:
    state = run.read_state()
    relative = state.get("run", {}).get("snapshot") or run.meta().get("snapshot_flow")
    if not relative:
        return None
    flow, _ = load_flow(run.path / str(relative))
    return flow


class RunsScreen(Screen[None]):
    """Active and recent runs; pending prompts first (§10.1)."""

    BINDINGS = [Binding("enter", "open", "Open"), Binding("s", "cycle_filter", "Filter status")]
    FILTERS = (None, "waiting", "running", "failed", "succeeded")

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.status_filter: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("", id="runs-filter")
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        table.add_columns("status", "run", "flow", "current", "duration", "spend", "waiting on")
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
            table.add_row(
                status_text(item["status"]),
                item["run_id"],
                item["flow"] or "",
                item["current"] or "",
                _duration(item),
                _money(item["totals"]["usd_spent"]),
                ", ".join(item["pending"]),
                key=item["run_id"],
            )
        if runs:
            table.move_cursor(row=min(cursor, len(runs) - 1))
        label = f"filter: {self.status_filter or 'all'}  ·  {len(runs)} run(s)"
        self.query_one("#runs-filter", Label).update(label)

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
    """Flow files under flow_paths (§10.1)."""

    BINDINGS = [Binding("enter", "open", "Graph")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="flows", cursor_type="row", zebra_stripes=True)
        yield Footer()

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

    def action_open(self) -> None:
        table = self.query_one("#flows", DataTable)
        if table.row_count:
            path = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            self.app.push_screen(FlowGraphScreen(Path(path)))

    @on(DataTable.RowSelected, "#flows")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(FlowGraphScreen(Path(str(event.row_key.value))))


class RunDetailScreen(Screen[None]):
    """One run: live graph, visit timeline, inspector, gauges (§10.1)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, config: Config, run_id: str) -> None:
        super().__init__()
        self.config = config
        self.run = RunDir(config.runs_dir / run_id)
        self.flow = run_flow_of(self.run)
        self.seen_events = -1
        self.selected: tuple[str, int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield Static("", id="banner")
        with Horizontal(id="gauges"):
            for name in ("usd", "tokens", "steps", "time"):
                with Vertical(classes="gauge"):
                    yield Label(name, id=f"gauge-{name}-label")
                    yield ProgressBar(id=f"gauge-{name}", show_eta=False)
        with Horizontal(id="body"):
            with VerticalScroll(id="graph-pane"):
                yield GraphView(id="graph")
            with Vertical(id="side"):
                yield ListView(id="timeline")
                with TabbedContent(id="inspector"):
                    for tab in ("prompt", "output", "activity", "logs", "artifacts", "usage"):
                        with TabPane(tab.capitalize(), id=f"tab-{tab}"), VerticalScroll():
                            yield Static("", id=f"inspect-{tab}")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_detail()
        self.set_interval(DETAIL_REFRESH_S, self.refresh_detail)

    def refresh_detail(self) -> None:
        events = read_log(self.run.events).events
        if len(events) == self.seen_events:
            return
        self.seen_events = len(events)
        state = self.run.read_state()
        info = runinfo.detail(self.run, Clock())
        self.query_one("#summary", Static).update(
            Text.assemble(
                status_text(info["status"]),
                f"  {info['run_id']}  ·  {info['flow']}  ·  {_money(info['totals']['usd_spent'])}",
                f"  ·  {info['totals']['tokens_spent']} tokens",
            )
        )
        banner = self.query_one("#banner", Static)
        pending = info["pending_human"]
        banner.update(
            Text(
                "\n".join(
                    f"? {node}: {str(prompt.get('message', '')).strip()}"
                    for node, prompt in pending.items()
                ),
                style="magenta bold",
            )
            if pending
            else ""
        )
        banner.display = bool(pending)
        if self.flow is not None:
            self.query_one("#graph", GraphView).show(
                self.flow, node_status(state), state.get("visits")
            )
            self._gauges(state)
        self._timeline(events)
        self._inspect()

    def _gauges(self, state: dict[str, Any]) -> None:
        assert self.flow is not None
        usd, tokens = run_limits(self.flow.limits)
        totals = state["totals"]
        max_steps = self.flow.limits.get("max_steps", 200)
        max_duration = parse_duration(self.flow.limits.get("max_duration", "8h"))
        values = {
            "usd": (totals["usd_spent"], usd),
            "tokens": (totals["tokens_spent"], tokens),
            "steps": (totals["steps"], None if max_steps == "none" else float(max_steps)),
            "time": (runinfo.summary(self.run, Clock(), state)["totals"]["active_s"], max_duration),
        }
        for name, (value, limit) in values.items():
            bar = self.query_one(f"#gauge-{name}", ProgressBar)
            bar.update(total=limit or None, progress=float(value or 0))
            shown = f"{value:.2f}" if isinstance(value, float) else str(value)
            self.query_one(f"#gauge-{name}-label", Label).update(
                f"{name} {shown}" + (f" / {limit:g}" if limit else "")
            )

    def _timeline(self, events: list[dict[str, Any]]) -> None:
        timeline = self.query_one("#timeline", ListView)
        timeline.clear()
        finished = {(e["node"], e["visit"]): e for e in events if e["type"] == "visit_finished"}
        for event in events:
            if event["type"] != "visit_started":
                continue
            key = (event["node"], event["visit"])
            done = finished.get(key)
            outcome = done["data"]["outcome"] if done else "running"
            label = Text.assemble(status_text(outcome), f"  {event['node']} #{event['visit']}")
            item = ListItem(Label(label))
            item.visit = key  # type: ignore[attr-defined]
            timeline.append(item)
        if self.selected is None and finished:
            self.selected = list(finished)[-1]

    @on(ListView.Highlighted, "#timeline")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            self.selected = getattr(event.item, "visit", None)
            self._inspect()

    def _inspect(self) -> None:
        if self.selected is None:
            return
        node, visit = self.selected
        result: dict[str, Any] = next(
            (
                v
                for v in (self.run.read_state()["nodes"].get(node) or {}).get("visits", [])
                if v.get("visit") == visit
            ),
            {},
        )
        prompt = runinfo.visit_files(self.run, node, visit, "prompt.md")
        texts = {
            "prompt": prompt[0].read_text(errors="replace") if prompt else "(no prompt)",
            "output": json.dumps(result.get("output"), indent=2)
            if result.get("output") is not None
            else (result.get("text") or "(no output)"),
            "activity": "\n".join(
                f"{e['data'].get('kind')}: {e['data'].get('summary')}"
                for e in runinfo.events(self.run, node, visit)
                if e["type"] == "progress"
            )
            or "(no activity)",
            "logs": "\n".join(
                f"--- {path.parent.name}/{path.name}\n{path.read_text(errors='replace')[-4000:]}"
                for name in ("stdout.log", "stderr.log")
                for path in runinfo.visit_files(self.run, node, visit, name)
            )
            or "(no logs)",
            "artifacts": "\n".join(
                a["path"] for a in runinfo.artifacts(self.run, node) if a["visit"] == visit
            )
            or "(no artifacts)",
            "usage": json.dumps(
                {
                    k: result.get(k)
                    for k in ("usage", "cost_usd", "num_turns", "session_id", "duration_s", "error")
                },
                indent=2,
            ),
        }
        for tab, text in texts.items():
            self.query_one(f"#inspect-{tab}", Static).update(Text(text))


class FlowGraphScreen(Screen[None]):
    """A flow's graph, live-synced with its file (§10.3)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.watcher = FileWatcher(path)
        self.flow: Flow | None = None
        self.previous: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(str(self.path), id="flow-title")
        with VerticalScroll(id="graph-pane"):
            yield GraphView(id="graph")
        yield Static("", id="problems")
        yield Footer()

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
