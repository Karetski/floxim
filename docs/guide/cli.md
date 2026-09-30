# Using the CLI

The `floxim` command (or its short alias `flx`) is the whole engine: the TUI and your scripts use the same operations and produce the same records. This page is organized by task; [spec §9](../spec.md#9-cli) is the complete reference, and `floxim <command> --help` lists every option.

## Set up a project

```sh
floxim init        # .floxim/config.yaml, .floxim/.gitignore and flows/hello.yaml
floxim doctor      # Python, git, harness binaries and versions
floxim adapters --probe
```

The project root is the nearest directory with `.floxim/`, else the git repository root, else the current directory; `--project DIR` overrides it. Configuration is read from `~/.config/floxim/config.yaml`, then `.floxim/config.yaml`, then `FLOXIM_*` environment variables, then `--config FILE` ([§2.2](../spec.md#22-configuration-files)).

## Check and look at flows

```sh
floxim flows                                  # flow files under flow_paths, with validity
floxim validate flows/*.yaml                  # errors and warnings, with line and column
floxim validate --strict flows/*.yaml         # warnings fail too (for CI)
floxim graph flows/implement-feature.yaml     # also --format mermaid, dot or json
```

## Run a flow

```sh
floxim run flows/implement-feature.yaml \
  --input feature="Add a --dry-run flag" --input branch=feat/dry-run
```

- Inputs come from `--input name=value` (repeatable; `name=@file` reads a file) or `--inputs-file inputs.yaml`.
- `--workdir DIR` runs the nodes in another directory (default: the project root).
- A foreground run prints its progress on stderr. Ctrl-C stops it and leaves it resumable (exit 8); a second Ctrl-C within 3 seconds skips the grace period.
- `--detach` starts it in the background and returns the run ID at once; `floxim wait <run>` blocks until it ends or waits.
- `--on-wait` chooses what happens at a human node: `prompt` asks on the terminal (the default with a terminal), `exit` releases the run and exits 4 (the default without one), `wait` keeps the process polling until someone answers elsewhere.
- `--allow-full` permits agent nodes with `permissions: full`.

## Follow and inspect runs

Every run has an ID such as `20260930T044930-hello-yomf`. Commands accept the full ID, any unique prefix, or `@last`.

```sh
floxim status                    # active runs: running, waiting, interrupted
floxim status @last              # one run: current node, visits, totals, pending prompt, outputs
floxim list --status failed --since 7d
floxim logs @last --follow       # the event log as it happens
floxim logs @last --node plan --prompt   # the exact prompt an agent received
floxim logs @last --node plan --raw      # the harness's raw stream
floxim artifacts @last
```

`floxim tui` shows the same information live, with a graph of the run ([TUI keys](#the-tui)).

## Answer a waiting run

```sh
floxim respond @last --choice merge
floxim respond @last approve --text "Use the v2 API instead."   # name the node when several wait
floxim respond @last --ack
```

`respond` records the answer and, when no runner holds the run, continues it in the background (`--no-continue` only records). An answer that does not fit the prompt exits 2. `floxim handoff @last` opens a pending handoff session in its harness; the flow continues when you exit it.

## Stop, resume and repair

```sh
floxim cancel @last --reason "wrong branch"
floxim resume @last                     # continue an interrupted or waiting run
floxim resume @last --from implement    # continue at another node
floxim resume @last --force             # reopen a failed or cancelled run
floxim resume @last --reload            # continue with the edited flow file
floxim resume --due                     # continue every run whose timeout or sleep has passed
floxim doctor @last                     # check and repair a run's log and state
```

Resume never reruns a finished step; a step cut off mid-way is started again or, with `on_resume: ask`, turned into a question ([§7.5](../spec.md#75-resume), [§7.6](../spec.md#76-resume-options)).

## Edit a flow from scripts

```sh
floxim flow add-node flows/f.yaml lint --type shell --after build --set run="npm run lint"
floxim flow connect flows/f.yaml build lint
floxim flow rename-node flows/f.yaml lint check
floxim flow set flows/f.yaml check timeout 10m
```

`connect --when EXPR` adds a conditional case and `--on-error` edits `on_error` instead of `next`. Edits keep comments and formatting, refuse to break a valid flow (exit 3), and refuse to overwrite a file that changed since it was read (exit 7). The operations are `add-node`, `rm-node`, `rename-node`, `set`, `unset`, `connect` and `disconnect` ([§10.4](../spec.md#104-structured-edits)).

## Clean up

```sh
floxim gc --dry-run                     # what would be deleted
floxim gc --older-than 14d --status succeeded,cancelled
```

## Scripting

- `--json` on any command prints exactly one JSON document to stdout: `{"ok": true, "data": …}` or `{"ok": false, "error": {"code": …, "message": …, "details": …}}`. Progress stays on stderr. `floxim schema cli` publishes the JSON Schema of every command's document.
- `--events` on `run`, `resume` and `logs` streams the event log as JSON lines instead.
- `--quiet`, `--verbose`, `--no-color` (or `NO_COLOR`) and `--as NAME` (who is answering or cancelling; default `$USER`) work before or after the command name.

| Exit code | Meaning |
|---|---|
| 0 | Success; for `run`, `resume` and `wait`, the run succeeded |
| 1 | The run failed |
| 2 | Usage error, or an answer that does not fit the prompt |
| 3 | Flow or inputs invalid |
| 4 | The run is waiting for a human |
| 5 | The run was cancelled |
| 6 | Not found: flow, run or node |
| 7 | Conflict: the run is held by a live runner, or the file changed during an edit |
| 8 | The runner stopped and left the run resumable |
| 70 | Internal error (please report it) |

The [scheduling guide](scheduling.md) puts these together for cron, systemd and CI.

## The TUI

`floxim tui` opens on the runs list; `floxim tui flows/f.yaml` opens a flow's graph editor and `floxim tui @last` a run. Keys are listed in the footer and in the command palette (`ctrl+p`):

| Where | Keys |
|---|---|
| Everywhere | `r` runs, `f` flows, `enter` open, `escape` back, `q` quit |
| Runs, run detail | `a` answer, `c` cancel, `u` resume; `s` filters runs by status |
| Run detail | `h` handoff, `g` open the selected visit's session in its harness, `y` copy its session ID, `o` open its artifact, `L` resume with the edited flow |
| Flows | `x` run (with a form for the inputs), `v` validate |
| Flow graph | `e` edit a field, `p` edit the prompt in `$EDITOR`, `n` add, `d` remove, `R` rename, `k` connect, `x` disconnect, `o` reorder cases, `t` change type |

The flow graph follows the file live, so edits made by hand, by an agent or by `floxim flow` show up within a moment ([§10](../spec.md#10-tui-live-graph-and-editing)).
