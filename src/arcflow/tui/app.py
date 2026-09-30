"""The Arcflow TUI application (spec §10)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.events import Resize
from textual.screen import Screen

from arcflow.config import Config
from arcflow.tui.screens import FlowGraphScreen, FlowsScreen, RunDetailScreen, RunsScreen

MIN_WIDTH, MIN_HEIGHT = 80, 24

CSS = """
#summary { padding: 0 1; height: auto; }
#banner { padding: 0 1; height: auto; background: $boost; }
#gauges { height: 3; }
.gauge { width: 1fr; height: 3; padding: 0 1; }
#body { height: 1fr; }
#graph-pane { width: 3fr; border: round $primary; }
#side { width: 2fr; }
#timeline { height: 1fr; border: round $primary; }
#inspector { height: 2fr; }
#node-list { height: 1fr; border: round $primary; }
#node-inspector { height: 2fr; border: round $primary; padding: 0 1; }
#problems { height: auto; max-height: 12; padding: 0 1; }
#runs-filter, #flow-title { padding: 0 1; }
"""


class ArcflowApp(App[None]):
    """Runs, flows, run detail and flow graphs, read from disk and refreshed live."""

    TITLE = "Arcflow"
    CSS = CSS
    BINDINGS = [
        Binding("r", "show_runs", "Runs"),
        Binding("f", "show_flows", "Flows"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, target: str | None = None) -> None:
        super().__init__()
        self.config = config
        self.target = target

    def on_mount(self) -> None:
        self.push_screen(RunsScreen(self.config))
        if self.target:
            path = Path(self.target)
            if path.suffix in (".yaml", ".yml") and path.exists():
                self.push_screen(FlowGraphScreen(path))
            elif (self.config.runs_dir / self.target).is_dir():
                self.push_screen(RunDetailScreen(self.config, self.target))

    def action_show_runs(self) -> None:
        """Back to the runs list, the screen under every other one."""
        while len(self.screen_stack) > 2:
            self.pop_screen()

    def action_show_flows(self) -> None:
        self.push_screen(FlowsScreen(self.config))

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("Runs", "Show active and recent runs", self.action_show_runs)
        yield SystemCommand("Flows", "Show the project's flow files", self.action_show_flows)

    def on_resize(self, event: Resize) -> None:
        if event.size.width < MIN_WIDTH or event.size.height < MIN_HEIGHT:
            size = f"{event.size.width}×{event.size.height}"
            self.notify(
                f"The terminal is {size}; Arcflow needs {MIN_WIDTH}×{MIN_HEIGHT}.",
                severity="warning",
            )
