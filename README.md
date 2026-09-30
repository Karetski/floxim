# Floxim

Floxim is a terminal-first workflow orchestrator for AI coding agents. You describe a flow (plan with one agent, implement with another, run the tests, loop until they pass, ask a person before merging) as a graph in one YAML file. Floxim runs it durably by driving the agent applications you already use, Claude Code and Codex CLI, in headless mode.

- **Flows are files.** One YAML file per flow, validated with line-and-column errors, editable by hand, from the TUI, from scripts, or by an agent, with comments and formatting kept.
- **Runs survive anything.** Every step is recorded before the next one starts. A crashed, killed or rebooted run resumes where it stopped, and a run can wait days for an approval with no process alive.
- **Harness-agnostic.** Mix Claude Code and Codex in one flow, or add any JSON-lines agent CLI with a short YAML file. Floxim never calls model APIs itself.
- **Bounded.** Budgets in USD and tokens, per node and per run; visit limits on every loop; timeouts on every step.
- **No daemon.** Schedule with cron, systemd or CI; stable exit codes and `--json` everywhere.

## Install

Floxim needs Python 3.10 or later on Linux or macOS, and git.

```sh
pipx install floxim        # or: uv tool install floxim
floxim --version
```

Every dependency is pure Python. There is also a single-file `floxim.pyz` on each [release](https://github.com/Karetski/floxim/releases) that runs with any Python 3.10+: `python3 floxim.pyz --version`.

To run agent nodes, install and log in to the harnesses your flows use: [Claude Code](https://code.claude.com/docs/en/overview) and/or [Codex CLI](https://github.com/openai/codex). `floxim adapters --probe` shows what Floxim can find.

## Quickstart

In a project (ideally a git repository):

```sh
floxim init
```

This creates `.floxim/config.yaml` and a first flow, `flows/hello.yaml`: a shell step that greets someone, then a question for you. It needs no agent and costs nothing. Run it:

```console
$ floxim run flows/hello.yaml --input name=Ada
run 20260930T045556-hello-ihqk
note: flows/hello.yaml is not tracked by git; a flow runs commands as you, so review it before running it
▶ greet (shell)
✓ greet succeeded (0.004s)
  → ask
▶ ask (human)

? ask: Greeted Ada. Finish?
  [finish/again]: finish
✓ ask succeeded (1.002s)
  → end
run succeeded
```

The note appears because the new file is not committed yet: a flow runs commands with your privileges, so Floxim points out flow files that git does not track.

Every run is recorded under `.floxim/runs/`. Look at it:

```sh
floxim status @last        # outcome of each step, totals and outputs
floxim logs @last          # the full event log
floxim tui                 # the live terminal UI
```

Runs do not need a terminal to wait. Without one (in cron or CI, or with `--on-wait exit`), a run that reaches a question stops with exit code 4, and anyone can answer later; the run then continues in the background:

```sh
floxim run flows/hello.yaml --input name=Ada --on-wait exit   # exits 4 at the question
floxim status                                                  # … waiting on ask
floxim respond @last --choice finish                           # answers and continues it
```

Next, look at [`examples/implement-feature.yaml`](examples/implement-feature.yaml): Claude Code plans, Codex implements, the tests loop back into the implementation until they pass, and a person approves the merge.

```sh
floxim graph examples/implement-feature.yaml
```

## Documentation

- [Writing flows](docs/guide/writing-flows.md): nodes, routing, expressions, agents, humans, budgets, workspaces.
- [Using the CLI](docs/guide/cli.md): running, inspecting, answering, resuming, editing, scripting, and the TUI.
- [Scheduling and unattended runs](docs/guide/scheduling.md): cron, systemd timers and CI recipes.
- [Security](docs/guide/security.md): permissions, injection-safe shell steps, secrets, at-least-once side effects.
- [Harnesses and adapters](docs/guide/adapters.md): Claude Code, Codex, command adapters, plugins and the conformance kit.
- [Examples](examples/): the flows from the spec's Appendix A.
- [Specification](docs/spec.md): the complete, authoritative definition of the flow format and behaviour. Design decisions are recorded in [`docs/adr/`](docs/adr/README.md).

## Status

Floxim is at its first release, 0.1. The flow format is version 1 (`floxim: 1`), and the CLI's exit codes and `--json` documents are part of its public interface. The Claude Code and Codex adapters are tested against recorded stream formats; see [Harnesses and adapters](docs/guide/adapters.md#the-conformance-kit) for what has been checked against live harnesses. Agent-authored flows (`floxim new`, `floxim edit`) are planned.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # create .venv with the locked dev dependencies
uv run pytest           # tests (never call a real harness or spend money)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type check
```

Start with [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) for goals and working agreements, and [`docs/milestones.md`](docs/milestones.md) for the plan.

## License

MIT, see [`LICENSE`](LICENSE).
