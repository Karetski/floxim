# Scheduling and unattended runs

Arcflow has no daemon and no scheduler (ADR 0008). A run is a directory under `.arcflow/runs/`, and any process can pick it up: the one that started it, `arcflow respond`, `arcflow resume`, or the TUI. That makes cron, systemd timers and CI enough to run flows on a schedule, and lets a run wait days for an approval with no process alive.

## The building blocks

- **Exit codes** say how a run ended ([spec §9.2](../spec.md#92-exit-codes)):

  | Code | Meaning |
  |---|---|
  | 0 | Succeeded |
  | 1 | Failed |
  | 3 | Flow or inputs invalid |
  | 4 | Waiting for a human |
  | 5 | Cancelled |
  | 8 | The runner detached; the run can be resumed |

- **`--json`** on every command prints one JSON document to stdout (`{"ok": …, "data": …}`); progress goes to stderr ([§9.1](../spec.md#91-conventions)). `arcflow schema cli` publishes the shapes.
- **`--on-wait exit`** makes a runner that reaches a human node release the run and exit with code 4. It is the default when stdin is not a terminal ([§6.11](../spec.md#611-waiting-for-humans-and-the-runner-process)).
- **`arcflow respond`** records an answer and, when no runner holds the run, starts a detached runner that continues it.
- **`arcflow resume --due`** continues every run whose human timeout or sleep has passed. Run it from cron so deadlines fire without anyone touching the run.
- **`--detach`** starts a run (or resume) in the background and returns at once; `arcflow wait <run>` blocks until it finishes or waits, with the same exit codes.

## cron

Run a flow every night, and fire due timeouts and sleeps every five minutes:

```cron
# m  h  dom mon dow  command
30   2  *   *   *    cd /srv/app && arcflow run flows/nightly-deps.yaml --on-wait exit --json >> .arcflow/cron.log 2>&1
*/5  *  *   *   *    cd /srv/app && arcflow resume --due --quiet
```

cron's environment is minimal: set `PATH` so it finds `arcflow`, `git` and the harness binaries, and make sure the harness is authenticated for that user (`arcflow adapters --probe` shows versions and login hints). Harness processes only see the variables Arcflow lets through ([security.md](security.md#environment)); add what your flow needs to `env_passthrough`.

A run that stops at an approval exits 4. Answer it whenever you like:

```sh
arcflow status                         # which runs are waiting, and on what
arcflow respond @last --choice merge   # records the answer and continues the run in the background
```

Use the `on_wait` hook to be told a run is waiting; it receives the details as `ARCFLOW_*` environment variables:

```yaml
# .arcflow/config.yaml
on_wait: 'notify-send "Arcflow" "$ARCFLOW_MESSAGE"'
```

## systemd timers

```ini
# ~/.config/systemd/user/arcflow-nightly.service
[Unit]
Description=Nightly dependency upgrade

[Service]
Type=oneshot
WorkingDirectory=%h/src/app
ExecStart=%h/.local/bin/arcflow run flows/nightly-deps.yaml --on-wait exit --json
# 4 = waiting for a human: not a failure of the unit
SuccessExitStatus=4
```

```ini
# ~/.config/systemd/user/arcflow-nightly.timer
[Timer]
OnCalendar=*-*-* 02:30
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `systemctl --user enable --now arcflow-nightly.timer`. A second pair of units running `arcflow resume --due` every few minutes (`OnCalendar=*:0/5`) fires deadlines, as in the cron recipe.

## CI

CI machines are ephemeral, so a run's directory disappears with the job. Use CI for flows that run start to finish without waiting on a person, as `nightly-deps` in [`examples/`](../../examples/) does: instead of an approval node, it opens a pull request that a person reviews. Keep the run directory as a build artifact for inspection.

```yaml
# .github/workflows/nightly-deps.yml
on:
  schedule: [{cron: "30 2 * * *"}]
  workflow_dispatch:

jobs:
  upgrade:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: pipx install arcflow
      - run: npm install -g @anthropic-ai/claude-code @openai/codex
      - run: arcflow validate --strict flows/nightly-deps.yaml
      - run: arcflow run flows/nightly-deps.yaml --on-wait exit --json
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          GH_TOKEN: ${{ github.token }}
      - if: always()
        uses: actions/upload-artifact@v7
        with:
          name: arcflow-runs
          path: .arcflow/runs/
```

The job fails when the run does not succeed (any non-zero exit). `GH_TOKEN` reaches the flow's shell nodes only if the project config lists it in `env_passthrough`.

`arcflow validate --strict` also makes a good CI check for a repository's flows: it fails on warnings such as unguarded loops or agent output interpolated into shell scripts.

## Keeping the runs directory tidy

Nothing is deleted automatically. `arcflow gc` removes finished runs older than `retention.keep_days` (30 by default) with their worktrees; waiting runs are never collected ([§7.7](../spec.md#77-retention)). Add it to the same crontab:

```cron
0 4 * * 0   cd /srv/app && arcflow gc --quiet
```
