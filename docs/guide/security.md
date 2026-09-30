# Security

Floxim runs shell commands and coding agents with your privileges. This page says what it protects against, what it does not, and the patterns that keep unattended flows safe. The rules are defined in [spec §12](../spec.md#12-security).

## A flow file is code

Running a flow is like running a Makefile: its shell and python nodes run as you, with no sandbox. Read a flow before running it, as you would a script. `floxim run` prints a notice when the flow file is not tracked by git.

Adapters, `python` nodes and plugin packages are code too. Installing one is trusting it; `floxim adapters` shows where each adapter comes from.

## Agent output is untrusted input

Agents read code, issues and web pages that may contain prompt injections, and what they write flows into later prompts, shell commands and human prompts. Floxim's defences:

- **Permission profiles** ([§8.4](../spec.md#84-permission-profiles)). Unattended agents never see an interactive permission prompt; anything that would prompt is denied. `read-only` cannot change files; `edit` (the default) can change files in its workspace and run only the commands you allow with `allow_tools`; `full` can do anything you can, so `floxim run` refuses it unless you pass `--allow-full` or set `allow_full: true` in the project config.
- **Structured output.** An `output_schema` makes the agent's answer data you can check and branch on, instead of free text.
- **Human checkpoints.** Put a `human` node before anything that is hard to undo: pushing, merging, deploying, deleting. `floxim validate` warns (`W-NO-HUMAN-BEFORE-RISKY`) about a shell step that matches a risky pattern (`git push`, `gh pr merge`, `kubectl`, `terraform apply`, `rm -rf`, …) and can be reached without passing a human node. Adjust the patterns with `risky_commands` in the project config.

## Never interpolate agent output into a shell script

`run:` is a shell script, so `${{ nodes.agent.text }}` inside it runs whatever the agent wrote. `floxim validate` warns about this (`W-SHELL-INTERPOLATION`). Pass values in one of these ways instead ([§12.5](../spec.md#125-injection-safe-interpolation)):

```yaml
# args: no shell at all
open_pr:
  type: shell
  args: [gh, pr, create, --title, "${{ nodes.plan.output.title }}", --body, "${{ nodes.plan.output.summary }}"]

# env: the value reaches the script as a variable
commit:
  type: shell
  env: { MESSAGE: "${{ nodes.implement.output.summary }}" }
  run: git commit -am "$MESSAGE"

# shq(): shell-quote the value when it has to be inline
tag:
  type: shell
  run: git tag ${{ shq(nodes.release.output.version) }}
```

The TUI strips terminal control sequences from agent text and never interprets markup in it.

## Environment

Harness and command processes start from an **empty** environment plus a small allowlist ([§12.3](../spec.md#123-environment-control)):

1. basics such as `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `TMPDIR`, proxy and certificate variables, and `GIT_*`;
2. each harness's own auth variables (`ANTHROPIC_API_KEY` and friends for Claude Code, `OPENAI_API_KEY` and `CODEX_API_KEY` for Codex);
3. the names you list in `env_passthrough` in the project config;
4. the flow's and node's `env`;
5. `FLOXIM_*` run variables.

So a token in your shell does not reach a flow's nodes unless you pass it on purpose. Variables that would make a harness think it runs inside another agent session (`CLAUDECODE`, `CODEX_THREAD_ID`, …) are always removed.

## Secrets in run directories

Floxim never writes environment values to its logs; `env` entries are recorded by name only. Rendered prompts and harness streams are stored as they are, because they are the audit trail, so they can contain secrets an agent printed. Run directories are private to your user (`0700`), and `.floxim/.gitignore` keeps `runs/` and `worktrees/` out of git.

The `redact` patterns in the project config are masked in everything Floxim writes itself: events, command output logs, the TUI, and raw harness streams once their attempt ends (unless `redact_streams: false`).

```yaml
# .floxim/config.yaml
redact: ['sk-[A-Za-z0-9_-]{20,}', 'ghp_[A-Za-z0-9]{36}']
```

## Side effects happen at least once

A run survives crashes by recording each step before moving on. A visit cut off in the middle (a crash, a reboot, a killed runner) is started again on resume, so a command it ran may run twice. Make shell steps idempotent where you can, and set `on_resume: ask` on the ones that must not repeat, so resume asks a person whether to rerun, skip or fail ([§7.5](../spec.md#75-resume)):

```yaml
deploy:
  type: shell
  run: ./deploy.sh production
  on_resume: ask
```

## Not in scope

Floxim does not isolate users from each other, protect run directories from the local user, or authenticate who answers a prompt: anyone who can write the run directory can respond, and the recorded responder is informational.

## Checking a flow

`floxim validate --strict` turns every warning into an error. Run it in CI on the repository's flows to catch shell interpolation, `permissions: full`, unbounded limits, unguarded loops and risky steps without a human checkpoint ([§12.7](../spec.md#127-lints)).
