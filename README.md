# Whisperwind

Whisperwind runs multi-step workflows for AI coding agents from the terminal. You write a **flow**, a graph of steps in one YAML file. A flow might be "plan with one agent, implement with another, run the tests, loop until they pass, ask me before merging". Whisperwind runs it by driving the agent CLIs you already use, **Claude Code** and **Codex CLI**, in headless mode. It never calls model APIs itself.

- **Flows are plain files.** You can edit them by hand, from the TUI or with `whw flow` commands, and comments and formatting are kept.
- **Runs are durable.** Every step is saved before the next one starts. A crashed or killed run resumes where it stopped. A run can wait days for your answer with no process running.
- **Runs are bounded.** Every run has budgets (USD and tokens), step and visit limits, and timeouts.
- **There is no daemon.** Schedule runs with cron, systemd or CI. Every command has stable exit codes and `--json` output.

## Install

You need Python 3.10+, git, and Linux or macOS.

```sh
pipx install whisperwind        # or: uv tool install whisperwind
whisperwind --version           # `whw` is a short alias
```

All dependencies are pure Python. Each GitHub release also ships a single-file `whisperwind.pyz`, which you run with `python3 whisperwind.pyz`.

To run agent steps, install and log in to [Claude Code](https://code.claude.com/docs/en/overview) and/or [Codex CLI](https://github.com/openai/codex). `whw adapters --probe` shows what Whisperwind can find.

## Quickstart

```sh
whw init                           # creates .whisperwind/ and flows/hello.yaml
whw run flows/hello.yaml --input name=Ada  # runs a shell step, then asks you a question
whw status @last                   # what happened
whw tui                            # the terminal UI
```

`hello.yaml` needs no agent and costs nothing. Every run is recorded under `.whisperwind/runs/<run-id>/`.

## A flow file

```yaml
whisperwind: 1
name: fix-tests
description: Let an agent fix failing tests, then ask before committing.

inputs:
  goal: { type: string, required: true }

limits:
  budget: { usd: 5 }

nodes:
  fix:
    type: agent
    harness: claude
    prompt: "Make the tests pass. Goal: ${{ inputs.goal }}"
    session: continue            # later visits continue the same agent session
    next: test

  test:
    type: shell
    run: npm test
    on_error: continue           # a failing test run is routed, not fatal
    next:
      - when: nodes.test.exit_code == 0
        to: approve
      - when: visits.fix < 3
        to: fix
      - to: fail

  approve:
    type: human
    message: Tests pass. Commit?
    choices: [commit, stop]
    next:
      - when: nodes.approve.choice == "commit"
        to: commit
      - to: end

  commit:
    type: shell
    run: git commit -am "Fix tests"
```

Here is how a flow file works:

- **Routing.**
  - `next` names the step to run after this one succeeds. It can also be a list of `when:` cases, where the first match wins.
  - `on_error` says what happens when a step fails: `fail` (the default), `continue`, the name of another step, or a list of cases.
  - `end` finishes the run successfully and `fail` ends it as failed.
- **Expressions** are a safe subset of Python. They read `inputs`, the results of earlier steps (`nodes.<id>`), `visits.<id>`, variables (`vars`), `run`, allowed `env` variables, and a set of helper functions such as `default`, `len`, `json`, `tail` and `shq`. Inside strings, `${{ … }}` inserts a value.
- **Top-level keys**:
  - `inputs`: typed run inputs with defaults.
  - `defaults`: shared settings for every step, or for every step of one type.
  - `limits`: `max_steps`, `max_duration` and `budget`.
  - `templates` and `include`: reusable step fragments, including fragments from shared files.
  - `outputs`: values the run returns.
  - `on_wait`: a command to run when a human step starts waiting.
- **Settings every step accepts**: `max_visits` (loop guard, default 10), `timeout`, `retry` (attempts and backoff), `on_resume` (`resume`, `restart` or `ask`), `workspace`, `env` and `description`.
- **Built-in limits**:
  - per run: 200 steps, 8 hours, $25 or 10M tokens
  - timeouts: agent steps 30m, shell steps 10m, human steps 7d

## Step types

| Type | What it does |
|---|---|
| `agent` | Runs a Claude Code, Codex or custom agent session with a prompt. Options include model, effort, permission profile, allowed and denied tools, extra directories, a per-step budget, and sessions (`new`, `continue`, `resume` or `fork` another step's session). An `output_schema` (JSON Schema) makes the agent's final answer structured JSON that is checked, with automatic retries to fix invalid output. |
| `shell` | Runs a script (`run`) or a program without a shell (`args`). Can parse stdout as JSON. Full output is saved to files. |
| `condition` | Only routes, based on expressions. |
| `human` | Waits for a person: a choice, free text, or an acknowledgement. Can have a timeout with a default answer. |
| `sleep` | Waits for a duration or until a time. |
| `set` | Sets variables. |
| `python` | Calls a Python function (`module:function`) in a child process and returns its JSON result. |
| `subflow` | Runs another flow file as a child run. |
| `map` | Runs a subflow once per item of a list, one item at a time. |
| `handoff` | Opens an agent step's session interactively in your terminal, then continues the flow when you exit. |
| `notify` | Sends a message through a command or a webhook. |

Steps run one at a time; there is no parallelism.

## Running flows

- **Run**: `whw run <flow> --input k=v`. Use `--input k=@file` to read a value from a file, or `--inputs-file`. Add `--detach` to run in the background.
- **Waiting for people.** When a run reaches a `human` step:
  - In a terminal, Whisperwind asks you inline.
  - Without a terminal (cron, CI, `--on-wait exit`), Whisperwind stops with exit code 4.
  - Anyone can answer later with `whw respond <run> --choice X` (or `--text`, `--ack`), or from the TUI. The run then continues in the background.
  - `whw resume --due` from cron applies expired human timeouts.
- **Resume**: `whw resume <run>` continues after a crash, kill or reboot. Steps that already finished are never rerun.
  - `--from <step>` jumps to a step.
  - `--reload` uses the edited flow file.
  - `--rerun` restarts an interrupted step.
  - `--force` reopens a failed or cancelled run.
  - Side effects of a step that was cut off halfway are not undone. Set `on_resume: ask` on steps that must not run twice.
- **Cancel**: `whw cancel <run>`. Ctrl-C only pauses a run, which can then be resumed.
- **Budgets.** Cost and tokens are tracked per step and per run, and the run stops when a budget is reached. USD figures are estimates. For harnesses that report only tokens, prices come from the config file.
- **Workspaces.** By default all steps share the working directory. `workspace: worktree` gives a step, or a named group of steps, its own git worktree under `.whisperwind/worktrees/`. Whisperwind never merges or pushes branches; your flow does that with shell steps.
- **Artifacts.** Each step gets an artifacts directory (`WHISPERWIND_ARTIFACTS_DIR`). List it with `whw artifacts <run>`.
- **Snapshots.** A run copies the flow and its files when it starts, so editing the flow never affects a run in progress.

## Agents and permissions

**Built-in harnesses**:

| Harness | What it is |
|---|---|
| `claude` | Claude Code |
| `codex` | Codex CLI |
| `fake` | Scripted test double: no processes, no cost |

**Permission profiles**, set per agent step. Anything that would prompt for approval is denied.

| Profile | What the agent can do |
|---|---|
| `read-only` | Cannot change files. |
| `edit` (default) | Can edit files in its workspace, and run only the commands you allow. |
| `full` | Can do anything you can. `whw run` refuses it unless you pass `--allow-full` or set `allow_full: true` in the config. |

**Other agent CLIs**:

- **Command adapters.** Add any agent CLI that prints JSON lines with a YAML file, `.whisperwind/harnesses/<name>.yaml`, which maps its command line and output events. No code needed.
- **Python plugins.** A package can register an adapter under the `whisperwind.adapters` entry point.
- **Conformance kit.** `whw adapter test <name>` checks an adapter against recorded streams. Add `--live` to check it against the real CLI, which costs money.

## Commands

All commands accept `--json`, `--project`, `--config`, `--quiet`, `--verbose`, `--no-color` and `--as <name>`. A run can be named by its ID, a unique prefix, or `@last`.

| Command | Purpose |
|---|---|
| `init` | Set up `.whisperwind/` and a starter flow. |
| `validate <flow>… [--strict]` | Check flows. Errors show line and column; `--strict` turns warnings into errors. |
| `graph <flow> [--format ascii\|mermaid\|dot\|json]` | Draw a flow's graph. |
| `flows` | List the flow files in the project. |
| `run`, `resume`, `wait` | Start a run, continue a run, wait for a run to finish or pause. |
| `status [<run>]`, `list` | Show one run in detail, the active runs, or run history. |
| `logs <run> [--follow] [--raw] [--prompt]` | Show the event log, raw agent output or the rendered prompt. |
| `respond`, `cancel`, `handoff` | Answer a waiting step, cancel a run, open a pending handoff session. |
| `artifacts <run>` | List a run's files. |
| `flow add-node\|rm-node\|rename-node\|set\|unset\|connect\|disconnect` | Edit a flow file. Comments and formatting are kept, renames update every reference, and an edit that would break the flow is refused. |
| `adapters [--probe]`, `adapter test` | List harness adapters; test one. |
| `schema flow\|config\|cli\|harness` | Print JSON Schemas, for example for editor autocompletion. |
| `gc` | Delete old finished runs and their worktrees. |
| `doctor [<run>]` | Check your setup, or repair a damaged run. |
| `tui [<flow>\|<run>]` | Open the terminal UI. |

**Exit codes**:

| Code | Meaning |
|---|---|
| 0 | OK / the run succeeded |
| 1 | The run failed |
| 2 | Bad usage or a bad answer |
| 3 | Invalid flow or inputs |
| 4 | Waiting for a person |
| 5 | Cancelled |
| 6 | Not found |
| 7 | Conflict: the run is locked, or the file changed during an edit |
| 8 | The run was interrupted and can be resumed |
| 70 | Internal error |

## Terminal UI

`whw tui` has four screens:

- **Runs**: live list of runs, with pending questions first.
- **Run detail**: a live graph and each step's prompt, output, logs, cost and artifacts.
- **Flows**: validate a flow, or start it with a form built from its inputs.
- **Graph editor**: stays in sync with the YAML file as you or an agent edit it.

Times show in your machine's local timezone; run IDs and stored timestamps stay in UTC. The runs list shows when each run started and finished: the time alone for today's runs, the date and minute for older ones. An active run has no finish time. In a narrow terminal, the list scrolls sideways while the status and run columns stay put. Run detail shows the full start time with the timezone's abbreviation and, once the run has finished, its finish time and how long it took. The timeline has a row per step: its status marker, when it started and, once it has finished, how long it took, what it cost, how many attempts it needed, and where the run went next (`→ test`). A failure the flow handled has a yellow marker and says how: `continued → ask` for `on_error: continue`, or `on_error → fix` for an `on_error` route.

The top of run detail sums up the run. While it's active, the summary names the step it's at. A run that failed or was cancelled shows where it stopped, who cancelled it, and why. A run's inputs are listed under a collapsed `inputs` row. One line compares what the run has spent with its limits, e.g. `$0.10 / $25  ·  420 / 10M tokens  ·  2 / 200 steps  ·  4s / 8h`. A limit that is at least half used gets a bar and a percentage. In the timeline, an unfinished step shows how the run stands: waiting for a person, running, interrupted when no runner is live, or cancelled. While the run is active, the graph marks that step the same way. The child runs of a `map` or `subflow` step are listed under it (`↳ ✓ item [2]  4s`, with the item's index for `map`), showing each one's status and, while it's active, the step it's at. Choosing one with `enter` or a click opens it.

The graph and the timeline are linked. The graph scrolls to the step the run is at, marks the node of the step selected in the timeline with a background, and dims the nodes the run has not reached. Until you pick a step, the selection follows the latest one as the run moves on; picking the latest step again resumes following.

The inspector shows the step selected in the timeline; the latest step is selected when the run opens. Its tabs show only what the step recorded: Prompt (an agent's prompt, or a person's question, the answer it takes and, once given, the answer), Output (any error, then the structured output, an agent's text, a `set` node's values, a condition's branch or a shell's exit code), Activity, Logs, Artifacts, and Usage (harness, session, turns, cost and tokens). Flow shows the edit to the flow file while it differs from the run's snapshot. A tab you pick stays open as you move between steps that have it; otherwise a failed step opens on its logs and any other step on its output. An unfinished step's logs update as they grow, and stay scrolled to the end unless you scroll up.

Runs and Flows are the two top-level views. Their keys, `r` and `f`, are pinned at the left of the footer on every screen, and the current view's key is highlighted. Each view keeps its place while you're in the other one: the selected row, the status filter, and any run or flow you opened from it. Pressing the key of the view you're already in goes back to its list.

From the TUI you can answer questions, cancel, resume, open handoffs, and open a step's session in its harness. Every action is a key, and `ctrl+p` opens the command palette.

## Configuration

`.whisperwind/config.yaml` is created by `init`. A per-user file, `~/.config/whisperwind/config.yaml`, applies under it. The config describes the environment, never flow behavior:

- `flow_paths`: where to look for flow files
- `harnesses`: binary paths and tested versions
- `env_passthrough`: extra environment variables allowed into steps
- `prices`: token prices, for USD estimates
- `on_wait`: the default hook for waiting human steps
- `retention.keep_days`: used by `gc`
- `redact`: patterns masked in logs
- `allow_full`: allow the `full` permission profile
- `risky_commands`: patterns for commands that should have a human approval before them

## Security

- Steps start from a clean environment, with only an allowlist of variables plus the auth variables each harness needs.
- Run directories are private (mode `0700`) and ignored by git.
- `validate` warns when:
  - agent output is pasted into a shell script without `shq(…)`
  - a risky command such as `git push` has no human approval before it
  - a loop is unbounded
  - a step uses the `full` profile
- Whisperwind notes when you run a flow file that git does not track.
- `python` steps, plugins and command adapters run with your privileges.

## Examples

`examples/` has four complete flows:

- [`implement-feature.yaml`](examples/implement-feature.yaml): plan, implement, loop on tests, approve.
- [`babysit-pr.yaml`](examples/babysit-pr.yaml): watch a PR's CI and have an agent fix failures.
- [`nightly-deps.yaml`](examples/nightly-deps.yaml): upgrade npm dependencies in a worktree, test, review, open a PR.
- [`triage-issues.yaml`](examples/triage-issues.yaml): label and answer new GitHub issues, running [`triage-one.yaml`](examples/triage-one.yaml) once per issue.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # create .venv with the locked dev dependencies
uv run pytest           # tests (never call a real harness or spend money)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type check
```

The repository is itself a Whisperwind project: `.whisperwind/config.yaml` configures it, and `flows/` holds flows for working on Whisperwind. `flows/tui-tour.yaml` runs through every part of the run detail screen at no cost, using the scripted `fake` harness. To get `whisperwind` and `whw` commands that run this checkout, with your edits live, install it in editable mode:

```sh
uv tool install -e .
whw tui                 # from the repository root: this project's flows and runs
```

TUI screens are checked against SVG snapshots in `tests/__snapshots__/`. After an intended screen change, run `uv run pytest --snapshot-update` and commit the updated snapshots. On every pull request, CI posts a comment showing each added, changed or removed screen as text, with a diff for the changed ones.

Releases are published to PyPI and GitHub by pushing a `vX.Y.Z` tag that matches `__version__` in `src/whisperwind/__init__.py`.

## License

MIT, see [`LICENSE`](LICENSE).
