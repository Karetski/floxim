# Floxim: Stage 1 Research

| | |
|---|---|
| Status | Final. Stage 1 closed 2026-09-30; decisions recorded in docs/adr/ |
| Date | 2026-09-29 |
| Scope | PROJECT_BRIEF.md §15, Stage 1 |
| Verified against | Claude Code 2.1.285 (run live), Codex CLI 0.159.1 and `@openai/codex-sdk` 0.159.1 (help output and SDK type definitions inspected, not run), current vendor docs as of today |

This document has four parts: harness headless interfaces (the highest-priority item), prior art, flow format and expression language, and TUI frameworks. It ends with proposed answers for the open decisions in brief §12. **Everything in §6 is a proposal for Alexey to decide**, not a decision; accepted items become ADRs in `docs/adr/`.

Confidence markers used below:

- **[tested]**: observed by running the tool in this session.
- **[docs]**: stated in current official documentation or `--help` output.
- **[source]**: read from the published package source or type definitions.
- **[reported]**: third-party report; not independently verified.
- **[inferred]**: my conclusion from the above; treat as a hypothesis to check in M3.

---

## 0. Summary

1. **Both harnesses are drivable headlessly through their CLIs, and their official SDKs are thin wrappers around those CLIs.** Claude's Agent SDK and the Codex SDK both spawn the CLI and parse its JSON stream. So Floxim loses nothing by talking to the CLIs directly from any language, which frees the language choice.
2. **Claude Code is the richer target.** It has schema-validated output, caller-chosen session IDs, a USD cost figure, a USD budget cap, a turn cap, tool allow/deny rules, and a documented way to route permission prompts to an external program. Codex gives schema output, resume, sandbox modes and token counts, but no cost figure, no budget cap, no turn cap, and no caller-chosen session ID.
3. **Neither harness enforces a wall-clock timeout, and budgets are soft.** In a test, a $0.001 cap stopped the run at $0.0052. Floxim must own timeouts, cancellation, and budget accounting, and treat harness caps as a second line of defence.
4. **Exit codes are not enough to judge success.** An interrupted Claude run exited 0 with an error result, and a Claude run that pushed its work to a background task reported `success` without doing the work. Adapters must read the final result event, and agent nodes that matter should require a structured `status` field.
5. **Running Floxim inside another agent session leaks that session's identity.** A nested `claude -p` reported the parent session's ID unless `--session-id` was passed explicitly. Adapters must set session IDs themselves and run harnesses with a controlled environment.
6. **The closest prior art is lionagi** (Python, Apache-2.0, cron triggers, drives Claude Code and Codex CLIs, YAML playbooks). It is a broad framework rather than a small declarative flow runner, so Floxim's gap still holds, but it deserves a hands-on trial before the spec. Several new script-based projects (Open Dynamic Workflows, pi-dynamic-workflows, dagents) confirm demand but none is declarative, file-based and terminal-first together.
7. **Proposed stack (revised after Alexey's notes, see §8):** Python 3.10+ with pure-Python dependencies, Textual for the TUI, one YAML flow file per flow as the single source of truth that the TUI, agents and the user all edit, harness adapters in four pluggable tiers, and one run directory per run with an append-only event log. Details and alternatives are in §6 and §8.

---

## 1. Harness headless interfaces

### 1.1 Capability matrix

| Capability | Claude Code 2.1.285 | Codex CLI 0.159.1 |
|---|---|---|
| Headless entry point | `claude -p` [docs, tested] | `codex exec` (alias `codex e`) [docs] |
| Official SDK | Agent SDK, TS and Python; the TS package 0.3.285 spawns a bundled per-platform Claude Code binary (`spawnClaudeCodeProcess`) [source] | `@openai/codex-sdk` (TS); spawns `codex exec --experimental-json` [source] |
| Rich bidirectional protocol | `--input-format stream-json` with `--output-format stream-json` (used by the SDK) [docs] | `codex app-server` JSON-RPC over stdio; officially "experimental and unsupported for production" [docs] |
| Event stream | `--output-format stream-json --verbose`, JSONL; last line is a `result` event [docs, tested] | `--json`, JSONL of `thread.started`, `turn.*`, `item.*`, `error` [docs, source] |
| Structured output | `--json-schema '<schema>'`; validated result in `structured_output` [docs, tested] | `--output-schema <file>`; final `agent_message` text is the JSON [docs, source] |
| Final text only | `result` field | `-o/--output-last-message <file>` |
| Session ID | Caller can set it: `--session-id <uuid>` [docs, tested] | Assigned by Codex; arrives in `thread.started.thread_id` [source] |
| Resume | `--resume <id>`; `--fork-session` for a copy [docs, tested] | `codex exec resume <id> [prompt]`, `codex exec fork` [docs] |
| Skip persistence | `--no-session-persistence` | `--ephemeral` |
| Cost | `total_cost_usd` and per-model `modelUsage[*].costUSD`, client-side estimates [docs, tested] | Token counts only (`turn.completed.usage`) [source]; no USD figure |
| Budget cap | `--max-budget-usd` (soft, see §1.4) [docs, tested] | None found |
| Turn cap | `--max-turns` (hidden from `--help`, documented) [docs] | None found |
| Wall-clock timeout | None | None |
| Permissions | Modes (`default`/`manual`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`), `--allowedTools`/`--disallowedTools` rules, `--tools` [docs] | `--sandbox read-only|workspace-write|danger-full-access`, `approval_policy` config, `--approve-for-me` (auto-review), exec-policy `.rules` [docs] |
| External approval handler | `--permission-prompt-tool <mcp tool>`; or `--permission-prompts none` to auto-deny [docs] | Approvals only through app-server requests (`item/permissions/requestApproval`) [docs] |
| Cancellation | SIGINT ends the turn cleanly; SIGTERM exits 143 with no result [docs, tested] | SDK sends SIGTERM (`child.kill()`) on abort [source]; CLI behaviour not tested |
| Interactive handoff | `claude --resume <id>` opens the same session in the TUI [docs] | `codex resume <id>` opens it in the TUI [docs] |
| Worktree | `-w/--worktree [name]` [docs] | `--worktree` [docs] |
| Working dir | Process cwd, plus `--add-dir` | `-C/--cd`, plus `--add-dir` |
| Instructions / context | `--append-system-prompt[-file]`, `--system-prompt[-file]`, `--agents`, `--plugin-dir`, `--mcp-config`, `--settings`, `--bare` [docs] | `-c key=value` overrides, `--profile`, `--ignore-user-config`, `--ignore-rules`, AGENTS.md [docs] |
| Model | `--model`, `--fallback-model`, `--effort` | `-m/--model`, `-c model_reasoning_effort=...` |
| Auth for automation | `ANTHROPIC_API_KEY` (required with `--bare`), or `claude setup-token` for subscription [docs] | `CODEX_API_KEY` for `codex exec` [docs] |

### 1.2 Claude Code in detail

**Invocation template the adapter should use** (all flags verified in `--help` or the CLI reference):

```
claude -p --bare \
  --session-id <uuid chosen by Floxim> \
  --output-format stream-json --verbose \
  --json-schema '<schema>' \
  --model <model> --max-turns <n> --max-budget-usd <usd> \
  --permission-mode dontAsk --permission-prompts none \
  --allowedTools "<rules>" \
  --append-system-prompt-file <node instructions> \
  < prompt.txt
```

Notes:

- **`--bare`** skips hooks, plugins, MCP servers, CLAUDE.md and auto-memory from the host, and is documented as "recommended for scripted and SDK calls" and "will become the default for `-p` in a future release." It requires `ANTHROPIC_API_KEY` (or an `apiKeyHelper`); subscription OAuth is not read. Floxim should make bare mode a per-node choice: bare for reproducible CI runs, non-bare when the user wants their repo's CLAUDE.md, skills and hooks. [docs]
- **Prompt via stdin.** With no stdin attached, `claude -p` waits 3 seconds and prints "no stdin data received in 3s" [tested]. Pipe the prompt in, or redirect stdin from `/dev/null`. Piped stdin is capped at 10 MB [docs].
- **Structured output** is produced through an internal tool call: the test run reported `num_turns: 2` and `stop_reason: "tool_use"` for a trivial prompt, with the object in `structured_output` and a JSON string in `result` [tested]. The schema is validated at startup ("`--json-schema is not a valid JSON Schema`") and `format` is not enforced [docs].
- **Result event fields** that matter for Floxim [tested]: `subtype` (`success`, `error_max_budget_usd`, `error_during_execution`, ...), `is_error`, `terminal_reason` (`completed`, `budget_exhausted`, `aborted_tools`), `session_id`, `total_cost_usd`, `usage`, `modelUsage`, `num_turns`, `duration_ms`, `permission_denials`, `result`, `structured_output`.
- **Other stream events** seen [tested]: `system/init` (tools, model, permission mode, MCP servers, version), `system/status`, `system/commands_changed`, `assistant`, `user` (tool results), `stream_event` (partial tokens), `rate_limit_event`. Docs add `system/api_retry` and `permission_denied`. `system/init` has a `capabilities` array for feature detection instead of version checks [docs].
- **Resume cost semantics.** A resumed run's `total_cost_usd` includes the whole session's earlier spend (since v2.1.277), while `--max-budget-usd` counts only the new call's spend [docs]. The adapter must store the previous total and report the delta.
- **Background work at exit.** Background Bash tasks are killed about 5 seconds after the final result; background subagents and workflows keep `-p` open for up to 10 minutes of idle waiting (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`) [docs]. In one test the agent moved a long command to the background and the run ended `success` with the work unfinished [tested, with the parent's `CLAUDE_AUTO_BACKGROUND_TASKS=true` inherited]. See §1.5.
- **SDK-only extras.** The Agent SDK adds a `canUseTool` callback and `interrupt()`. Through the CLI, the same outcomes come from `--permission-prompt-tool` (an MCP tool Floxim could host) and SIGINT. [docs]
- **Dynamic workflows** can start inside a `-p` run if permissions allow the `Workflow` tool [docs]. Floxim should deny it by default (`--disallowedTools Workflow`) so a node does not silently fan out into dozens of agents.

### 1.3 Codex in detail

**Invocation template** (flags from `codex exec --help`, 0.159.1):

```
codex exec --json \
  -C <workdir> --sandbox workspace-write \
  -c approval_policy="never" \
  --output-schema <schema.json> -o <last-message.txt> \
  -m <model> --skip-git-repo-check \
  - < prompt.txt
```

Resume: `codex exec resume <thread_id> -` with the follow-up prompt on stdin.

Notes:

- **Event types** from the SDK's type definitions [source]: `thread.started {thread_id}`, `turn.started`, `turn.completed {usage}`, `turn.failed {error}`, `item.started|updated|completed {item}`, `error {message}`. Item kinds: `agent_message`, `reasoning`, `command_execution` (command, aggregated output, exit code, status), `file_change`, `mcp_tool_call`, `web_search`, `todo_list`, `error`. This maps cleanly onto Floxim's `stream()` events.
- **Usage** has `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` [source]. No USD. Floxim needs its own price table to show cost, or budgets in tokens.
- **Session ID** is only known after `thread.started`. The adapter must persist it the moment that event arrives so a crash after that point can still resume.
- **Approvals in exec.** The default approval policy in `codex exec` is reported to be `never`, which auto-rejects anything that needs approval. One report on 0.153.4 shows MCP tool calls failing with "MCP tool call requires approval, but approval policy is never," which the agent cannot distinguish from a dead server [reported]. The adapter should set the approval policy and per-MCP-server approval explicitly rather than rely on user config.
- **Default sandbox.** The non-interactive docs say `codex exec` runs read-only by default; the security docs say workspace-write is the default. The two pages disagree [docs], so the adapter should always pass `--sandbox`. Network is off by default [docs].
- **Structured output limits.** OpenAI's strict structured-output mode is widely reported to require `additionalProperties: false` and every property listed in `required` [inferred from OpenAI API behaviour; verify with Codex in M3]. Floxim's schema validator should warn when a schema used with Codex breaks those rules.
- **`codex app-server`** offers `thread/start`, `thread/resume`, `thread/fork`, `turn/start`, `turn/steer`, `turn/interrupt`, per-turn `outputSchema`, and server-to-client approval requests. It is the only way to answer Codex approvals from Floxim, but OpenAI labels it experimental and recommends the SDK for automation [docs]. Keep it out of v1.
- **Not verified here:** how `codex exec` reacts to SIGINT vs SIGTERM, its exit codes on failure, and whether `codex resume <id>` opens an exec-created session interactively. No Codex credentials were available in this environment. These go into the M3 contract tests.

### 1.4 Tested behaviours worth designing around (Claude Code)

All runs used `--model haiku` and cost about $0.10 in total.

| Test | Result |
|---|---|
| `--json-schema` with a one-field schema | `subtype: success`, `structured_output: {"ok": true}`, `num_turns: 2` |
| Nested run without `--session-id` | Reported the **parent session's ID**. Unsetting `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION` and `CLAUDECODE` did not change that. `--fork-session` without `--session-id` did the same. |
| `--session-id <uuid>` then `--resume <uuid>` | The resumed run recalled the earlier turn. Session ID preserved. |
| `--resume <id> --fork-session --session-id <new>` | New ID used, history carried over. |
| `--max-budget-usd 0.001` | Exit 1, `subtype: error_max_budget_usd`, `terminal_reason: budget_exhausted`, actual spend **$0.0052** (5× the cap). The cap is checked after a response, not before. |
| SIGINT during a 40 s tool call | Exited within ~1 s, **exit code 0**, final `result` with `subtype: error_during_execution`, `is_error: true`, `terminal_reason: aborted_tools`. Child process killed. |
| SIGTERM during a 40 s tool call | Exit 143, **no result event**. Child process killed. Matches docs: the turn is left unfinished and can be resumed. |
| Long foreground command, parent env inherited | Agent backgrounded it and returned `success` with the work not done. |

### 1.5 Adapter design implications

1. **Adapters own the process.** Spawn the harness in its own process group; on cancel send SIGINT, wait a grace period (e.g. 10 s) for the final result, then SIGTERM, then SIGKILL the group. Record which step ended it.
2. **Success is judged from the result event, not the exit code.** Map `(exit code, last result event, schema validation)` to Floxim's node outcome. A missing result event is a failure.
3. **Floxim chooses session IDs where it can** (Claude) and **captures them immediately** where it cannot (Codex), writing them to the node's checkpoint before the run finishes.
4. **Controlled environment.** Start from a clean env plus an allowlist (PATH, HOME, auth variables, proxy settings), plus per-node `env`. Never pass through `CLAUDE_CODE_*`, `CLAUDECODE`, `CODEX_*` session variables from a parent agent. This matters because the brief plans to dogfood Floxim from inside agent sessions after M4.
5. **Timeouts and budgets live in Floxim.** Enforce wall-clock timeouts in the runner. Enforce budgets by summing per-node spend and refusing to start a node that would start over budget; pass the remaining budget to Claude's `--max-budget-usd` as a backstop and expect overshoot of one model response.
6. **Budgets in tokens and USD.** Subscription users (Claude login, ChatGPT login) don't pay per token, and Codex reports no USD, so budgets should accept tokens, USD, or both. USD figures shown by Floxim are estimates, and should be labelled that way.
7. **Structured status for important nodes.** Encourage agent nodes to use an output schema with an explicit `status` (`done`, `blocked`, `failed`) so "success but didn't do it" is caught by branching instead of passing silently.
8. **Contract tests with recorded streams.** Record real JSONL streams per harness version (success, schema output, budget stop, SIGINT, SIGTERM, auth failure) and replay them through the adapter parser. The fake adapter from the brief can replay the same fixtures. Pin tested versions and read `claude_code_version` from `system/init` and `codex --version` to warn on untested versions.
9. **Resume policy for a node interrupted mid-run** needs a spec decision: rerun from scratch (simple, idempotent prompts) or resume the harness session with a "continue" prompt (keeps context, may repeat side effects). Proposal: default to resume-session when a session ID was recorded, with `on_resume: restart` available per node.

### 1.6 Other harnesses (for adapter shape, not v1)

- **Gemini CLI** has `-p` with `--output-format json|stream-json`, events `init`, `message`, `tool_use`, `tool_result`, `error`, `result`, per-model token stats, and documented exit codes (0, 1, 42 input error, 53 turn limit) [docs]. Same shape as the other two.
- **Agent Client Protocol (ACP)** is a JSON-RPC-over-stdio protocol between editors and agents: `session/new`, `session/load`, `session/prompt`, `session/cancel`, `session/request_permission`, `session/update` [docs]. Gemini CLI supports it natively; Claude and Codex via adapters. It covers cancellation and permission requests well but has **no structured output and no usage or cost reporting** [docs]. Proposal: not the primary adapter path, but a generic ACP adapter is a cheap way to support many harnesses later.

---

## 2. Prior art deep-dive

### 2.1 Claude Code dynamic workflows

A JavaScript script with `export const meta = {name, description}` and a body using `agent()`, `parallel()`, `pipeline()`, `phase()`, `log()` and an `args` global. Saved under `.claude/workflows/` and run as `/<name>` [docs].

What to borrow:
- **Replay-based resume.** On relaunch each agent call returns its saved result until the first call whose prompt differs; that one and everything after it rerun. `Date.now()`, `Math.random()` and `new Date()` throw inside scripts to keep replays deterministic [docs]. This is the Temporal model applied to agent calls, and it is how Floxim should think about checkpoints: record inputs and outputs per node visit, and reuse on resume only when inputs match.
- **Schema output with retries.** `agent(prompt, {schema})` retries until the output validates, up to 5 attempts (`MAX_STRUCTURED_OUTPUT_RETRIES`), and rejects self-contradictory schemas before running [docs].
- **Hard caps by default.** 16 concurrent agents, 4,096 items per fan-out, 1,000 agents per run [docs]. Matches brief principle 6.
- **Progress view** listing phases with agent counts, tokens and elapsed time, with drill-down and stop/restart keys [docs]. A good reference for Floxim's run detail screen.

Gaps relative to Floxim: Claude-only, no mid-run human input ("For sign-off between stages, run each stage as its own workflow"), resume only within the same Claude session, and code rather than a declarative graph [docs].

### 2.2 lionagi

Python, Apache-2.0, about 400 stars and 5,200 commits. Drives Claude Code, Codex, Pi and others as subprocesses. Flows are Python code, with YAML "playbooks" as well. CLI `li` covers agents, orchestration, scheduling (`li schedule create` for cron and interval) and monitoring; runs are saved under `~/.lionagi/runs/<id>/` and branches resume with `li agent -r` [docs, from the repo README]. There is also a web UI (Lion Studio).

Assessment: the most complete existing tool and the main competitor. It is a general multi-agent framework with a Python API, not a small declarative flow format, and its human-in-the-loop story was not visible in the README. **Recommendation: spend an hour trying lionagi's playbooks on the brief's `implement-feature` example before writing the spec**, and record what it does better.

### 2.3 Open Dynamic Workflows and similar

Open Dynamic Workflows (TypeScript, MIT, about 100 stars) runs the Claude workflow script dialect across Codex, Claude, Gemini, Qwen, Kimi, opencode, Cursor and others through adapters configured as command templates (`{workspace}`, `{prompt_file}`) [docs, repo README]. Its README states that completed calls are not journaled for replay after a crash, and budgets are estimated as characters ÷ 4, with running calls allowed to exceed them [docs]. pi-dynamic-workflows adds journaled resume, worktree isolation and cost accounting for the Pi harness. **dagents** (Apache-2.0, very early) is a web-canvas DAG tool that drives local CLIs; its human nodes live in memory and are lost on restart [docs, repo README].

Takeaways: command-template adapters are an attractive escape hatch for "any CLI harness"; durability is where these tools are weakest, which supports Floxim's focus on it.

### 2.4 LangGraph (state and checkpointing)

- State is checkpointed per step under a `thread_id`; the checkpointer gives conversation continuity, human-in-the-loop, time travel and fault tolerance [docs].
- `interrupt()` saves state and waits indefinitely; `Command(resume=value)` makes `interrupt()` return that value. **On resume the whole node reruns from the start**, so side effects before `interrupt()` must be idempotent [docs].

Takeaways for Floxim: keep the unit of durability the node visit; make human nodes their own node type (not a call inside an agent node) so nothing reruns around them; expose state snapshots for `floxim status` and a later "fork from step N".

### 2.5 Temporal (durable execution)

- Durability comes from an append-only **event history** that is replayed to rebuild state; workflow code must be deterministic; side effects happen in **activities** with retry policies and timeouts; external input arrives as **signals** [docs, general].

Takeaways: `events.jsonl` should be the source of truth, with `state.json` a derived snapshot that can be rebuilt from events. Agent and shell nodes are "activities": at-least-once, with retry policy and timeouts. `floxim respond` is a signal.

### 2.6 GitHub Actions (YAML ergonomics)

What people already know: `${{ expr }}` interpolation, `if:` conditions written without the braces, `needs:` for dependencies, step `id`s with `steps.<id>.outputs.<name>`, `env`, `timeout-minutes`, `continue-on-error`, and `matrix` for fan-out. Known pain points: string-only outputs, a small bespoke expression language, and no loops.

Takeaways: reuse the `${{ }}` delimiter and the `nodes.<id>.outputs` addressing, but use a real expression language with typed JSON values, and allow cycles with visit limits.

---

## 3. Flow format and expression language

### 3.1 File format

| Option | For | Against |
|---|---|---|
| **YAML** | Familiar from GitHub Actions, Compose, Kubernetes; block strings suit prompts; JSON Schema gives editor completion via yaml-language-server | Implicit typing pitfalls (mostly YAML 1.1 parsers), indentation errors |
| TOML | Unambiguous types | Deeply nested graphs and multi-line prompts read poorly |
| CUE / Pkl / Starlark | Validation and reuse built in | Unfamiliar; raises the bar for a "readable text file" |
| JSON | Trivial to parse | Unpleasant to write by hand, no comments |

**Proposal: YAML**, parsed as YAML 1.2, with a published JSON Schema for the flow file and a strict mode that rejects unknown keys. Long prompts can live in separate files (`prompt_file: prompts/plan.md`).

### 3.2 Expressions and templates

| Option | Typed, safe, non-Turing-complete | Go | Rust | TS | Notes |
|---|---|---|---|---|---|
| **CEL** | Yes | `cel-go` v0.30.0 (Google) | `cel` 0.14.5 | `@marcbachmann/cel-js` 8.0.0 | Used by Kubernetes admission policies and Google Cloud IAM. Type-checks against declared variables at validate time |
| expr-lang | Mostly | `expr` v1.17.8 | none | none | Go only |
| JMESPath | Query only | old (2020) | 0.5.0 | 0.16.0 | Awkward for boolean conditions |
| jq | Turing-complete | `gojq` v0.12.19 | `jaq` 3.1.1 | via wasm | Powerful but hard to read in a condition |
| Jinja-style templates | No (logic in templates) | `gonja` v2.9.1 | `minijinja` 2.24 | `nunjucks` | Good for prompt text, second language to learn |

**Proposal: CEL everywhere.** Conditions (`when:`) are bare CEL; strings interpolate `${{ cel }}`. One language, typed against the flow's declared inputs and output schemas, so `floxim validate` can catch `nodes.plan.output.stpes` before a run. Helpers like `json()`, `default()` and `truncate()` are added as CEL functions. If prompts need loops later, add an explicit `template: jinja` option per field rather than making it the default.

### 3.3 State and addressing model (proposal for the spec)

- `inputs.<name>`: run inputs.
- `nodes.<id>.output`: validated structured output of the latest visit (object), plus `text`, `exit_code`, `stdout`, `stderr`, `session_id`, `usage`, `cost_usd`, `status`, depending on node type.
- `nodes.<id>.visits[i]`: earlier visits, so loops can compare attempts.
- `visits.<id>`: visit count.
- `vars.<name>`: set by a future `set` node.
- `run.id`, `run.flow`, `run.started_at`: metadata.

Latest-visit-wins semantics keeps the common case (`nodes.test.stderr` in a fix loop) short, as in the brief's example.

---

## 4. TUI framework comparison

| | Go + Bubble Tea v2 | Rust + Ratatui | TypeScript + Ink |
|---|---|---|---|
| Current version | `charm.land/bubbletea/v2` v2.0.10 (2026-09-24) | 0.30.2 (2026-06-19) | 7.1.1 (2026-07) |
| Model | Elm architecture; Bubbles components (list, table, viewport, text input), Lip Gloss styling | Immediate-mode widgets; you own the event loop | React components |
| Live logs | Viewport component, easy with goroutines feeding messages | Straightforward, manual scrolling | Fine for moderate volume; React re-render cost on large logs |
| Graph rendering | No mature DAG widget; `mermaid-ascii` exists as a Go CLI | `ascii-dag` 0.11 crate, `tui-nodes` | None mature |
| Distribution | Single static binary, trivial cross-compile | Single static binary, cross-compile needs more setup | Needs Node, or `bun build --compile` (large binaries) |
| Harness SDKs | CLI only (fine: SDKs wrap the CLI) | CLI only | Official Claude Agent SDK and Codex SDK available |
| Ecosystem fit for Floxim | `cel-go` (reference implementation), `go.yaml.in/yaml/v3`, `santhosh-tekuri/jsonschema/v6`, strong process and signal handling | `cel` crate, `serde_yaml` is deprecated (successors `serde_yml`, `serde-saphyr`), `jsonschema` crate is strong | CEL port is third-party; Node process handling is adequate |

Graph rendering is the weak spot in every ecosystem. For v1, a vertical list of nodes in topological order with status icons and the edge taken (the same shape as Claude's `/workflows` view) is enough; `floxim graph --mermaid` covers the full picture outside the TUI.

---

## 5. Risks surfaced by the research

| Risk | Evidence | Mitigation |
|---|---|---|
| Harness flags and events change monthly | Claude docs cite behaviour changes across dozens of 2.1.x versions | Pinned versions, recorded-stream contract tests, feature detection via `system/init.capabilities` |
| Silent partial success | §1.4 background-task test | Structured `status` field, result-event parsing, env control |
| Budget overshoot | §1.4 budget test | Floxim-side accounting before each node start |
| Nested-agent identity leakage | §1.4 session-ID test | Always set session IDs, controlled env |
| Codex approvals auto-reject | [reported] on 0.153.4 | Explicit approval config in the adapter; surface `permission_denials`-style info in the node result |
| Vendor overlap | Claude workflows already offer replay-resume, schema output and progress views | Stay cross-harness, file-based, human-aware, CLI-scriptable |

---

## 6. Proposed decisions for brief §12

Each is a proposal for Alexey. Accepted ones become ADRs.

| # | Decision | Proposal | Main reason | Runner-up |
|---|---|---|---|---|
| 1 | Implementation language | **Python 3.10+ with Textual and pure-Python dependencies** (revised after Alexey's notes, see §8.3). Go with Bubble Tea v2 is the fallback if compiled code shipped through package managers is acceptable | Installs from any internal PyPI mirror with no compiled code; the engine is I/O-bound, so Python costs ~150 ms of CLI startup and microseconds per expression; plugin adapters and custom nodes are native Python | Go, then TypeScript + Ink |
| 2 | Flow format and expressions | **One YAML file per flow as the single source of truth, edited by the TUI, agents and the user alike (ruamel.yaml round-trip keeps comments); composition features (`subflow`, `map`, includes) live in the format itself; restricted Python-syntax expressions in `when:` and `${{ }}`** (revised twice, see §8.2) | Every editor can read and write a data file safely; Alexey ruled out a second representation that can't be written back | CEL, if native wheels are acceptable |
| 3 | State and addressing | **`inputs`, `nodes.<id>.output`, `nodes.<id>.visits[]`, `visits.<id>`, `vars`, `run`; latest visit wins** | Matches the brief's example; typed | GitHub-style string outputs (too weak) |
| 4 | Workspace isolation | **Shared working dir by default; opt-in `workspace: worktree` per node, managed by Floxim** | Sequential flows want to see each other's changes; worktrees become necessary with parallelism. Floxim should manage worktrees itself rather than use each harness's `--worktree`, so it works across harnesses | Worktree per run |
| 5 | Permissions | **Per-node profile (`read-only`, `edit`, `full`) mapped by each adapter, plus harness-specific passthrough. Unattended default: `edit`, prompts auto-denied** | One vocabulary across harnesses; Claude `dontAsk` + `--permission-prompts none`, Codex `workspace-write` + approval `never` | Later: route permission prompts to a human node via `--permission-prompt-tool` (Claude) and app-server (Codex) |
| 6 | Human node delivery | **CLI and TUI in v1, plus an optional `on_wait` shell command hook** for notifications | Keeps the core small; a hook covers desktop notifications, Slack or email without built-in integrations | Built-in `notify` node later |
| 7 | Parallelism in v1 | **No**, but design the event log and state for it (node visits keyed by branch) | Parallel branches force worktrees, merge policy and concurrent logs; M0–M8 are already full | `parallel`/`join` in a post-v1 milestone |
| 8 | Scheduling | **External only** (cron, systemd timers, CI), with stable exit codes and `--json` output, and docs recipes for each | Brief principle 7; no daemon | `floxim schedule` generating crontab/systemd units later |
| 9 | Name | **Defer to a separate short task.** "Loom" collides with the Loom video product (Atlassian) and many existing repos, so a crate/npm/Go module and domain check is needed | Not blocking for spec | — |
| 10 | License | **MIT** as the brief expects; Apache-2.0 if an explicit patent grant matters | Most common for CLI tools | Apache-2.0 |

Additional proposals the spec should cover:

- **Run directory:** `.floxim/runs/<run-id>/events.jsonl` (append-only, source of truth), `state.json` (derived snapshot), `nodes/<id>/<visit>/` for prompts, raw harness streams, stdout/stderr and artifacts.
- **Resume semantics:** node visits are at-least-once. A completed visit is never rerun on resume. An in-flight agent visit resumes its harness session by default (`on_resume: restart` to rerun instead).
- **Adapter interface additions** to the brief's sketch: `capabilities()` (schema output, cost, budget, resume, fork, interactive, permission hook), a `session_started` event emitted as soon as an ID is known, and a normalized `outcome` (`succeeded`, `failed`, `cancelled`, `budget_exceeded`, `timed_out`, `schema_invalid`).

---

## 7. Open questions to settle in M3 contract tests

1. Codex exit codes and SIGINT/SIGTERM behaviour for `codex exec`.
2. Whether `codex resume <id>` opens an exec-created session interactively.
3. Which JSON Schema features `codex exec --output-schema` rejects.
4. Default sandbox for `codex exec` (docs disagree).
5. Whether Claude's `--max-turns` stop produces a distinct result subtype.
6. What exactly makes a nested `claude -p` inherit the parent session's ID (only the workaround was tested).

---

## 8. Follow-up on Alexey's notes (2026-09-29)

Alexey asked for (1) a harness-agnostic adapter layer that users can extend with their own harnesses, (2) flows that agents can author, possibly composed in Python like lionagi, with a live TUI graph view that stays in sync with the file in both directions, and (3) an assessment of Python as the implementation language, with enterprise portability and performance as the criteria.

### 8.1 Pluggable harness adapters

The adapter interface from §1.5 becomes a public contract with a `capabilities()` declaration, so the engine can degrade gracefully. For example: no native structured output means Floxim extracts JSON from the final text and validates it itself; no resume means an interrupted node restarts; no cost means token-only budgets.

Four tiers, from cheapest to most powerful:

| Tier | What the user writes | Covers |
|---|---|---|
| 1. Built-in | Nothing | `claude`, `codex`, `fake` (replays recorded streams for tests) |
| 2. Command adapter | A YAML file in `.floxim/harnesses/<name>.yaml`: the command template (`{prompt_file}`, `{schema_file}`, `{session_id}`, `{cwd}`), how to resume, and field mappings from the harness's JSONL stream to Floxim events (session ID, final text, usage, error) | Any CLI with JSON output: Gemini CLI, opencode, Cursor CLI, an internal enterprise tool. Open Dynamic Workflows uses the same idea (§2.3) |
| 3. Plugin adapter | A Python class implementing the adapter protocol, registered as a `floxim.adapters` entry point in any installed package; or an executable in any language that speaks a small **Agent Adapter Protocol** (JSONL over stdio: `start`, `event`, `result`, `cancel`) | Harnesses needing real logic, such as an SDK, a websocket, or approvals |
| 4. ACP adapter | Nothing: point it at any ACP agent | Agents speaking the Agent Client Protocol (§1.6). No structured output or cost, so capabilities are limited |

Contract tests from §1.5 become a published test kit, so the author of a tier 2 or 3 adapter can run the same conformance suite as the built-ins.

### 8.2 Flows authored by agents, with a live TUI graph

**The tension.** A flow written as Python code (lionagi, Airflow, Prefect) is flexible, but it can't be safely edited by a TUI and written back, and previewing it means executing arbitrary code. A data file can be rendered, validated, edited and written back without running anything. Live two-way sync between the TUI, an agent and the user's editor needs the data file.

**Proposal: the YAML file is the single source of truth. The TUI, agents and the user's editor all read and write that same file, and nothing generates it.**

- **No second representation.** An earlier draft proposed an optional Python builder API that generates the YAML. Alexey rejected it (2026-09-29): anything that produces the flow file becomes a second source that TUI and agent edits can't flow back into. Composition needs are met inside the file format instead: `subflow` (call another flow file), `map` (run a subgraph per list item), reusable node `templates` with overrides, and `include` of shared fragments. The TUI and agents edit these like any other key.
- **Python code sits beside the graph, not around it.** Floxim can read and edit a `python` node's reference but not the function body; the user and agents edit that module as ordinary code.
- **Custom logic without code in the file.** A `python` node type references a function (`call: mypkg.checks:coverage_ok`), so real logic lives in normal Python modules while the graph stays declarative.
- **Agents author flows.** `floxim new "plan with Claude, implement with Codex, loop on tests"` runs an agent node whose job is to write the YAML. Its loop is write, run `floxim validate --json`, fix, repeat, guided by the published JSON Schema. Agents are reliable with schema-backed YAML plus a validator giving precise errors. `li o flow` in lionagi has an LLM plan a DAG from a prompt at run time; Floxim's version saves the plan as a file the user can review and rerun.
- **Live graph in the TUI.** The TUI watches the flow file (polling every ~300 ms needs no native dependency) and re-renders on every valid save. Invalid saves keep the last good graph and show the validation errors inline. Layout uses a layered (Sugiyama) algorithm; the pure-Python `grandalf` package laid out the brief's example, including its test→implement cycle, in a quick test.
- **Edits from the TUI** (rename a node, change an edge, edit a prompt) are written back with `ruamel.yaml`'s round-trip mode, which keeps the user's comments, key order and formatting, and uses atomic writes. If the file changed on disk since the TUI last read it, the TUI reloads and asks before overwriting.
- **"Edit with agent" inside the TUI.** The user types an instruction; Floxim runs a harness through the same adapter layer with the flow file as its target and `floxim validate` as its check; the graph updates live as the agent saves; the TUI shows a diff with accept or revert. Because it is just a file, the same loop works when the user edits in vim or another agent edits it from outside.

This keeps brief principle 1 ("the flow file is the source of truth") and lifts the v1 non-goal "visual graph editing in the TUI" only as far as structured edits and agent-driven edits. Free-form drag-and-drop layout stays out of scope.

### 8.3 Python as the implementation language

**Performance.** Floxim's engine spends almost all of its time waiting on harness processes that run for minutes. Measured in this environment (Python 3.11, median of 7 runs):

| Operation | Time |
|---|---|
| Bare interpreter start | 11 ms |
| CLI imports (ruamel.yaml, fastjsonschema, typer, expression evaluator) | ~150 ms (Go binary: a few ms) |
| TUI import (`textual.app`) | ~220 ms |
| Condition evaluation, `simpleeval` | ~20 µs |
| Condition evaluation, `cel-python` | ~200 µs |

The one place Python costs something is CLI startup, noticeable only when a script calls `floxim status --json` in a tight loop. Parsing a verbose harness stream is well within Python's JSON throughput. **Conclusion: no meaningful performance hit for this workload.**

**Enterprise portability.** Environments that block downloaded executables usually still allow packages from an internal PyPI or npm mirror. To stay installable where compiled code is also restricted, every runtime dependency should be pure Python:

| Need | Pure-Python choice | Avoid (ships compiled code) |
|---|---|---|
| YAML, round-trip | `ruamel.yaml` 0.19 (no dependencies) | PyYAML's C extension (it has a pure fallback) |
| JSON Schema | `fastjsonschema` | `jsonschema` (pulls in `rpds-py`, a Rust extension) |
| Expressions | Python-syntax subset via `simpleeval` or a small `ast`-whitelist evaluator | `cel-python` (pulls in `google-re2` and `pendulum`, both native) |
| TUI | `textual` 8.x (pure) | — |
| CLI | `typer` or `argparse` | — |
| File watching | Polling | `watchfiles` (Rust) |
| Validation models | dataclasses | `pydantic` v2 (`pydantic-core` is Rust) |

Install paths: `pipx install floxim` or `uv tool install floxim` for an isolated environment; a single-file `.pyz` zipapp for machines with only a system Python. Floor of Python 3.10 is proposed (Textual needs 3.9, `fastjsonschema` 3.10). Enterprise Linux images typically offer a 3.9+ interpreter through their app streams, but this should be confirmed for Alexey's target environments.

**Expressions change with Python.** CEL's only full Python implementation pulls in native code, so the proposal switches conditions to a restricted Python-expression subset (`nodes.test.exit_code == 0 and visits.implement < 3`). It is familiar to Python users and to agents, evaluates in microseconds, and blocks function calls and attribute tricks by default (tested: `__import__('os')` was rejected). The cost is losing CEL's type-checking at validate time. Floxim can recover most of it by checking that every `nodes.<id>.<field>` reference exists in the referenced node's output schema.

**Other Python advantages for Floxim:** plugin adapters and custom nodes load as ordinary Python packages via entry points; Floxim's own flow model is a plain Python library the TUI, CLI and validator share; Textual is the most capable TUI toolkit of the three compared in §4 (widgets, mouse, CSS-style layout, a built-in log widget).

**Costs to accept:** Python-version and dependency-conflict issues on user machines (mitigated by pipx/uv isolation and the zipapp); no first-party Python SDK for Codex (not needed, since Floxim drives CLIs); a slightly slower CLI start.

**Revised recommendation: Python 3.10+, pure-Python dependencies, Textual.** Go stays the fallback if the answer to one question is "compiled code via package managers is fine": the target users already install Claude Code (a native binary) and Codex (a Rust binary shipped through npm), and a Go binary can be shipped the same way (PyPI wheel or npm package, as `ruff` and Codex do). The question for Alexey: is the enterprise restriction about *downloaded executables*, or about *any compiled code*, including native wheels from a package mirror?

---

## Sources

Harness docs and tools
- Claude Code headless mode: https://code.claude.com/docs/en/headless
- Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Claude Agent SDK cost tracking: https://code.claude.com/docs/en/agent-sdk/cost-tracking
- Claude Code dynamic workflows: https://code.claude.com/docs/en/workflows
- `claude --help`, Claude Code 2.1.285 (run in this environment)
- Codex non-interactive mode: https://learn.chatgpt.com/docs/non-interactive-mode
- Codex app server: https://learn.chatgpt.com/docs/app-server
- Codex approvals and security: https://learn.chatgpt.com/docs/agent-approvals-security.md
- `codex exec --help`, Codex CLI 0.159.1; `@openai/codex-sdk` 0.159.1 `dist/index.d.ts` and `dist/index.js`
- Codex exec approval report: https://github.com/LeonJoeeee/devstandard/issues/358
- Gemini CLI headless: https://geminicli.com/docs/cli/headless/
- Agent Client Protocol: https://agentclientprotocol.com/protocol/overview and https://agentclientprotocol.com/get-started/agents

Prior art
- lionagi: https://github.com/ohdearquant/lionagi
- Open Dynamic Workflows: https://github.com/xz1220/open-dynamic-workflows
- pi-dynamic-workflows: https://github.com/QuintinShaw/pi-dynamic-workflows
- dagents: https://github.com/dagents/dagents
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Temporal workflow execution: https://docs.temporal.io/workflow-execution

Libraries (versions from proxy.golang.org, crates.io and npm on 2026-09-29)
- Bubble Tea v2, Ratatui, Ink, cel-go, cel (Rust), cel-js, expr, gojq, jaq, JMESPath, gonja, minijinja, go.yaml.in/yaml/v3, serde_yaml (deprecated), santhosh-tekuri/jsonschema, jsonschema (Rust), ascii-dag, tui-nodes
- mermaid-ascii: https://github.com/AlexanderGrooff/mermaid-ascii
- Python packages checked on PyPI 2026-09-29: textual 8.2.8, ruamel.yaml 0.19.1, fastjsonschema 2.22.2, simpleeval 1.0.8, cel-python 0.5.0, grandalf 0.8, lionagi 0.35.2
