# Harnesses and adapters

Arcflow is not an agent. It drives complete agent applications, called harnesses, in headless mode, through an adapter per harness. A flow names the harness on each agent node (`harness: claude`), so one flow can plan with Claude Code and review with Codex. The adapter contract is defined in [spec §8](../spec.md#8-harness-adapters).

`arcflow adapters` lists the adapters a project can use, with their source and capabilities; `arcflow adapters --probe` also runs each binary to report its version, whether that version is in the tested range, and whether it looks logged in.

## Built-in adapters

| Harness | Needs | Notes |
|---|---|---|
| `claude` | [Claude Code](https://code.claude.com/docs/en/overview), tested with 2.1.285 | Reports cost in USD, supports budgets, turn caps, tool rules, resume and fork. Uses the install's own login, or `ANTHROPIC_API_KEY` with `bare: true` on the node. |
| `codex` | [Codex CLI](https://github.com/openai/codex), tested with 0.147.0 | Reports tokens, not USD: set `prices` in the project config for USD estimates, or budget in tokens. Network access is off unless `harness_options: {network: true}`. |
| `fake` | nothing | Deterministic scripted responses, no processes, no cost. For testing flows and Arcflow itself. |

Point Arcflow at a binary that is not on `PATH`, or change the tested range, in the project config:

```yaml
harnesses:
  claude: { command: /opt/claude/bin/claude, tested_versions: ">=2.1.285,<2.2" }
```

A version outside `tested_versions` produces a warning, not an error. Harness CLIs change often; the [conformance kit](#the-conformance-kit) is how an adapter is checked against a new version.

`harness_options` passes adapter-specific settings from a node ([§8.2](../spec.md#82-harness_options), [§8.3](../spec.md#83-built-in-adapters)); `validate` checks them against the adapter's schema.

## Command adapters: any JSON-lines CLI, no code

A YAML file in `.arcflow/harnesses/<name>.yaml` turns any agent CLI that prints JSON lines into a harness ([§8.5](../spec.md#85-tier-2-command-adapters)). It says how to invoke the CLI, and maps its stream events to Arcflow's session ID, text, usage, result and errors with the same expression language flows use, with `event` bound to each parsed line:

```yaml
name: gemini
command: [gemini, -p, "{prompt}", --output-format, stream-json, -m, "{model}"]
prompt_via: argv
resume_command: [gemini, --resume, "{session_id}", -p, "{prompt}", --output-format, stream-json]
capabilities: { structured_output: false, resume: true, tokens: true, cost_usd: false }
stream:
  format: jsonl
  session_id: { when: 'event.type == "init"', value: event.session_id }
  text: { when: 'event.type == "message" and event.role == "assistant"', value: event.content }
  usage: { when: 'event.type == "result"', input_tokens: event.stats.input_tokens, output_tokens: event.stats.output_tokens }
  result: { when: 'event.type == "result"', success: 'event.status == "success"', text: event.response }
  error:  { when: 'event.type == "error"', message: event.message }
permissions:
  read-only: [--approval-mode, plan]
  edit: [--approval-mode, auto_edit]
env: [GEMINI_API_KEY]
tested_versions: ">=0.9"
```

This example shows the format; check the CLI's own documentation for its current flags and stream format before relying on it.

- Placeholders (`{prompt}`, `{prompt_file}`, `{schema_file}`, `{session_id}`, `{cwd}`, `{model}`, `{attempt_dir}`) are substituted per argument, never through a shell. A `[flag, "{x}"]` pair is left out when `x` is unset.
- The keys of `permissions` are the profiles the adapter supports; a node asking for another one is a validation error.
- `env` lists the auth variables the CLI needs; nothing else from your environment reaches it.
- The engine supplies process handling, timeouts, cancellation, budgets and JSON extraction.

`arcflow schema harness` prints the file's JSON Schema, and validating a flow checks the harness files it uses.

## Python adapters

A package can provide an adapter class through the `arcflow.adapters` entry-point group ([§8.6](../spec.md#86-tier-3-plugin-adapters)):

```toml
[project.entry-points."arcflow.adapters"]
myharness = "mypkg.adapter:MyAdapter"
```

Arcflow loads it only when a flow names that harness. The class implements the contract in `arcflow.adapters`: capabilities, a request-to-process mapping, stream parsing into normalized results, and interactive commands for `handoff`. Plugins are trusted code.

Resolution order is built-ins, then `.arcflow/harnesses/*.yaml`, then entry points.

## The conformance kit

`arcflow adapter test <name>` checks an adapter against recorded harness output ([§8.8](../spec.md#88-conformance-kit)):

- **Offline** (the default, free): replays fixture streams covering success, structured output, schema failure, budget stop, interrupted and killed runs, auth failure and a missing result through the adapter, and compares each normalized result with `expected.yaml`. Command adapters keep their fixtures in `.arcflow/harnesses/<name>/fixtures/`, one `<case>.jsonl` per case.
- **Live** (`--live`, opt-in, **spends money**): runs a small prompt set against the real harness with a spend cap, and checks session IDs, resume, structured output, cancellation and environment isolation.

The command exits 0 when every case passes and 1 otherwise, so it fits in CI. Re-record fixtures when you raise `tested_versions`.

The fixtures shipped with Arcflow for Claude Code 2.1.285 and Codex 0.147.0 were written from the harnesses' documented stream formats, not recorded from real runs; the live suite is how they get replaced with recordings.
