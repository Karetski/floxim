# Writing flows

A flow is one YAML file: a named graph of nodes. Floxim validates it, snapshots it when a run starts, and executes it one node at a time, recording every step so a run can be inspected and resumed. This guide is a tour; [the spec](../spec.md) is the complete reference, and each section below links to the part that defines it.

Start from `floxim init`, which writes `flows/hello.yaml`, or from the flows in [`examples/`](../../examples/). Point your editor's YAML language server at `floxim schema flow` for completion and inline errors, and run `floxim validate` often; every problem it reports has a stable code, a line and a column.

## The shape of a file

```yaml
floxim: 1
name: fix-tests
description: Ask an agent to fix failing tests, retrying until they pass.

inputs:
  test_command: { type: string, default: npm test }

limits:
  budget: { usd: 5 }

nodes:
  test:
    type: shell
    run: ${{ inputs.test_command }}
    on_error: continue          # a failing test run is data, not a run failure
    next:
      - when: nodes.test.exit_code == 0
        to: end
      - when: visits.fix < 3
        to: fix
      - to: fail
        reason: Tests still fail after three fixes.

  fix:
    type: agent
    harness: claude
    session: continue           # each fix keeps the context of the previous one
    prompt: |
      The tests fail. Fix the code, not the tests.
      ${{ tail(nodes.test.stdout + nodes.test.stderr, 100) }}
    next: test
```

The top-level keys are `name`, `nodes` (required), `description`, `inputs`, `defaults`, `limits`, `templates`, `include`, `start` and `outputs` ([§3.2](../spec.md#32-top-level-keys)). The file is strict: unknown keys are errors, except keys starting with `x-`. Relative paths in a flow (`prompt_file`, `output_schema`, subflows) resolve against the flow file's directory.

## Nodes

| Type | Use it to |
|---|---|
| `agent` | Run a Claude Code, Codex or other harness session headlessly ([§5.1](../spec.md#51-agent-core)). |
| `shell` | Run a command with `run:` (a `bash -eo pipefail` script) or `args:` (no shell) ([§5.2](../spec.md#52-shell-core)). |
| `condition` | Branch without doing work. |
| `human` | Pause for a choice, free text or an acknowledgement ([§5.4](../spec.md#54-human-core)). |
| `sleep` | Wait for a duration or until a time. |
| `set` | Assign run variables (`vars.*`). |
| `python` | Call a Python function in your repository. |
| `subflow` / `map` | Run another flow as a child run, once or once per list item. |
| `handoff` | Hand an agent session to you interactively; the flow continues when you exit. |
| `notify` | Send a notification through a command or webhook. |

Every node takes the common keys of [§3.8](../spec.md#38-nodes-common-keys): `description`, `next`, `on_error`, `timeout`, `retry`, `max_visits`, `on_resume`, `env` and `workspace`.

## Routing

`next` is either a node ID or a list of cases evaluated in order; the first case whose `when` is true wins, and a case without `when` is the default and must come last. The targets `end` and `fail` finish the run; a case to `fail` can carry a `reason`. A node without `next` goes to `end` ([§3.9](../spec.md#39-routing-next)).

When a node fails (a non-zero exit, a timeout, an agent error), `on_error` decides what happens: `fail` (the default) fails the run, `continue` routes with `next` as if it succeeded, and a node ID or a list of cases routes like `next` ([§6.5](../spec.md#65-errors-and-on_error)). Test steps in a fix loop want `on_error: continue`; `floxim validate` warns when a `next` branches on `exit_code` that `on_error: fail` would never let it see.

## Expressions and templates

`when` conditions are expressions; most string fields are templates that embed expressions as `${{ … }}` ([§4](../spec.md#4-expressions-and-templates)). The language is small and safe: comparisons, `and`/`or`/`not`, `in`, conditional expressions (`a if cond else b`), indexing, and functions such as `len`, `tail`, `join`, `pluck`, `round`, `shq` and `now` ([§4.2](../spec.md#42-functions)). It reads the run's state:

| Name | Holds |
|---|---|
| `inputs.*` | The run's inputs. |
| `nodes.<id>.*` | The latest result of a node: `outcome`, `output`, `text`, `exit_code`, `stdout`, `stderr`, `choice`, … |
| `visits.<id>` | How many times a node has been visited. |
| `vars.*` | Variables assigned by `set` nodes. |
| `run.*` | Run metadata, including `run.id` and `run.budget`. |
| `env.*` | Environment variables allowed into nodes. |

A node that has not run yet is `null`, so `nodes.review and nodes.review.output.verdict == "changes"` is safe on the first visit. `validate` checks that every reference names something that exists.

## Loops

Loops are edges back to an earlier node. Each node allows 10 visits by default (`max_visits`) and a run allows 200 in total (`limits.max_steps`), so a runaway loop always stops. Guard loops with `visits.<id>` in a `when` so they route somewhere useful, such as a human escalation, before the hard limit ([§6.4](../spec.md#64-loops-and-visit-limits)).

## Agents

```yaml
plan:
  type: agent
  harness: claude              # or codex, or a command adapter (see adapters.md)
  model: sonnet
  permissions: read-only       # read-only | edit (default) | full
  prompt: Write a plan for ${{ inputs.feature }}. Do not change files.
  output_schema: schemas/plan.json
  timeout: 30m
  budget: { usd: 2 }
```

- **Structured output.** With `output_schema`, the agent's final answer must be JSON that validates against it, and it is available as `nodes.plan.output`. Floxim asks the agent to fix an invalid answer (`schema_retries`) before failing the visit. Require a `status` field and branch on it, so an agent can say it is blocked rather than pretend it succeeded ([§5.1.2](../spec.md#512-structured-output)).
- **Sessions.** `session: new` (the default) starts fresh; `continue` resumes this node's previous session on later visits; `session: {resume: plan}` and `session: {fork: plan}` start from another node's latest session (same harness) ([§5.1.3](../spec.md#513-sessions)).
- **Permissions.** Unattended agents never get an interactive prompt: anything that would ask is denied. `edit` can change files in the workspace and run only commands listed in `allow_tools`; `full` needs `--allow-full` ([§8.4](../spec.md#84-permission-profiles)).

## Humans

```yaml
approve:
  type: human
  message: Tests pass. Merge ${{ inputs.branch }}?
  show: ["Estimated spend (USD): ${{ round(run.budget.usd_spent, 2) }}"]
  choices: [merge, reject]
  timeout: 3d
  default: reject
```

A human node pauses the run until someone answers from the terminal, `floxim respond` or the TUI. The answer is `nodes.approve.choice` (or `.text` with `input: text`). With a `timeout` and a `default`, the default is taken when the time passes; without a default, the timeout is an error routed by `on_error` ([§5.4](../spec.md#54-human-core), [§6.11](../spec.md#611-waiting-for-humans-and-the-runner-process)). The [scheduling guide](scheduling.md) shows how runs wait without a process.

## Limits, budgets and retries

```yaml
limits:
  max_duration: 2h
  max_steps: 100
  budget: { usd: 10, tokens: 5_000_000 }
```

The run budget is a hard stop: once spent, the next agent node fails the run. A node's own `budget` caps one visit. USD comes from the harness when it reports cost, otherwise from `prices` in the project config; it is always shown as an estimate ([§6.8](../spec.md#68-budgets-and-cost)). When nothing else is set, a run's budget is $25 and 10M tokens.

`retry: {max_attempts: 3, backoff: 30s}` repeats a failed visit; `timeout` limits each attempt ([§6.6](../spec.md#66-retries), [§6.7](../spec.md#67-timeouts-and-cancellation)).

## Workspaces

Nodes run in the run's working directory by default, and see each other's changes. `workspace: worktree` gives a node its own git worktree, and a named worktree (`workspace: {worktree: upgrade}`) is shared by every node that names it. Floxim creates worktrees under `.floxim/worktrees/` and never merges, pushes or deletes branches itself; flows do that in shell nodes ([§6.9](../spec.md#69-workspaces)).

## Reuse

`templates` defines partial nodes that nodes `extends`, and `include` pulls templates from shared files ([§3.6](../spec.md#36-templates-and-extends), [§3.7](../spec.md#37-include)). `subflow` and `map` run another flow file as a child run with its own inputs and outputs ([§5.8](../spec.md#58-subflow-core), [§5.9](../spec.md#59-map-core)).

## Resuming and side effects

A run survives crashes: `floxim resume <run>` continues from the last recorded step and never reruns a finished visit. A visit that was cut off in the middle is started again, so its side effects can happen twice (at-least-once). Set `on_resume: ask` on shell steps that must not repeat, such as a deploy ([§7.5](../spec.md#75-resume)).

## Editing

The file is the flow: edit it by hand, from the TUI, with `floxim flow <op>`, or with an agent. The structured edits (`add-node`, `rm-node`, `rename-node`, `set`, `unset`, `connect`, `disconnect`) keep comments and formatting and refuse an edit that would break a valid flow ([§10.4](../spec.md#104-structured-edits)). Runs use their snapshot, so editing a flow never changes a run in progress; `floxim resume --reload` continues a run with the edited file.
