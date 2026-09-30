# Project Brief: Arcflow

> **Purpose of this document.** This is the single entry point for any person or AI agent joining the project. It explains what we're building, why, what we already know, what's decided, what's still open, and how work is organized. Read it fully before doing anything else. When this brief and a later document (spec, ADR) disagree, the later, more specific document wins. Update this brief when major decisions change.

---

## 1. Status

| Item | State |
|---|---|
| Current stage | **Stage 4: Build**. Milestones M0–M8 are implemented; release 0.1.0 is prepared and is published by pushing its tag (`docs/releasing.md`). Stage 1 (Research) completed 2026-09-30, Stage 2 (Spec) and Stage 3 (Build phases) completed 2026-09-29 |
| Spec | `docs/spec.md`, accepted 2026-09-29; review decisions in its §15 |
| Build plan | `docs/milestones.md`, accepted 2026-09-29 |
| Code | `src/arcflow/`, version 0.1.0; user guides in `docs/guide/`, examples in `examples/` |
| Name | Arcflow (ADR 0013); CLI `arcflow`, short alias `arcf` |
| Last updated | 2026-09-29 |

---

## 2. One-paragraph summary

Arcflow is a **terminal-first, harness-agnostic workflow orchestrator for AI coding agents**. Users define **flows**: graphs of **nodes** (run an agent, run a shell command, branch on a condition, wait for a human, sleep, loop) connected by **edges**. Agent nodes don't call model APIs directly; they drive existing agent harnesses such as **Claude Code** and **OpenAI Codex CLI** in headless mode. Flows are plain text files that live in a repo, can be version-controlled, and run from bash. A **CLI** is the engine and the scripting surface; a **TUI** sits on top for running, monitoring, and managing flows interactively.

---

## 3. The problem

Coding agents (Claude Code, Codex) are very capable in a single session, but real work often needs more than one session:

- **Chaining:** plan with one agent, implement with another, review with a third.
- **Looping:** implement → run tests → fix → repeat until green, with a retry limit.
- **Branching:** take different paths depending on results.
- **Human checkpoints:** pause for approval before risky steps, or hand control to a human mid-flow.
- **Scheduling:** run a flow nightly, hourly, or on an event.
- **Durability:** a 40-minute flow that dies at step 9 should resume at step 9, not restart.

Today people do this with ad-hoc bash scripts, copy-pasting between sessions, or tools that each cover only part of the need.

### Target user

A developer who lives in the terminal, already uses Claude Code and/or Codex, and wants repeatable multi-step agent workflows without adopting a heavyweight platform, a web UI, or a single vendor.

---

## 4. Landscape (research done so far)

> Findings from web research on 2026-09-29. This space moves fast; **verify before relying on any detail** (see Stage 1).

### 4.1 Libraries that call model APIs directly (not harnesses)

- **LangChain:** open-source (MIT) toolkit for building LLM apps; chains steps together with integrations for models, tools, and data.
- **LangGraph:** from the same team; models an app as a graph of nodes and edges over shared state, supporting loops, conditional edges, checkpoints/persistence, human-in-the-loop interrupts, and streaming. **This is the conceptual model closest to Arcflow**, but it orchestrates raw model API calls, not harness sessions, and it's a Python/JS library, not a CLI/TUI tool.
- **Deep Agents** (LangChain): Claude Code-style agent built on LangGraph, still on raw APIs.

**Takeaway:** borrow LangGraph's concepts (state, conditional edges, checkpoints, interrupts). Don't reimplement an agent.

### 4.2 Tools that sit on top of harnesses

| Tool | What it does | Gap relative to Arcflow |
|---|---|---|
| **Claude Code dynamic workflows** | JS scripts using `agent()`, `parallel()`, `pipeline()`, `phase()` to orchestrate Claude subagents. | Claude-only, runs inside Claude Code's private runtime, and is code rather than a declarative graph. |
| **Claude Code Routines / scheduled tasks** | Saved prompt plus repo config with scheduled, API, or GitHub triggers (cloud); local scheduled tasks in Desktop; `/loop` in-session. | Claude-only; a single session per trigger, not a graph. |
| **Claude Code Agent Teams / subagents** | In-session parallelism and delegation. | Claude-only; no durable flow definition. |
| **Open Dynamic Workflows, codex-dynamic-workflows, pi-dynamic-workflows** | Re-host the Claude Code workflow script dialect on other backends (Codex, Gemini, etc.). | Script-based, not graph and file-based; no human-node or scheduling focus. |
| **lionagi** | Python runtime with DAG flows, cron/interval/GitHub-poll triggers, and on_fail/on_success chains; can use the Claude Code or Codex CLIs. | Closest existing tool. Evaluate its maturity, UX, and whether it's terminal-first. |
| **Conductor, Parallel Code, Superset, Orca** | GUIs for running many agents in parallel in git worktrees. | Parallel sessions, not flows; GUI rather than CLI. |
| **Tembo, Copilot coding agent, Codex web** | Background or cloud agents triggered from Slack, Linear, or GitHub. | Hosted, ticket-driven, not user-defined graphs. |
| **Temporal, Prefect, n8n** | General workflow engines. | Could wrap harnesses, but heavy and not agent-aware. Borrow ideas such as durable execution. |

### 4.3 The gap Arcflow fills

No tool found is **all** of the following:
1. **Harness-agnostic:** Claude Code, Codex, and future harnesses through adapters.
2. **Declarative and file-based:** flows are readable text files in the repo.
3. **Terminal-first:** a scriptable CLI plus a TUI, with no server or web UI required.
4. **Durable:** checkpointed, resumable runs.
5. **Human-aware:** first-class approval and handoff nodes.

### 4.4 Competitive risk

Anthropic and OpenAI are actively adding orchestration and scheduling to their own products. Arcflow's durable differentiators are **cross-harness, local, file-based, and scriptable**. Don't compete on features the vendors will ship natively.

---

## 5. Design principles

1. **The flow file is the source of truth.** Everything (CLI, TUI, scheduler) reads the same file. The TUI never holds state that isn't expressible in the file.
2. **The CLI is the engine; the TUI is a client.** Anything the TUI can do, the CLI can do. Every command supports `--json` output for scripting.
3. **Don't build an agent.** Agent work is delegated to harnesses through adapters. Arcflow orchestrates.
4. **Structured data between nodes.** Agent nodes can be required to return JSON matching a schema. Branching uses fields, never free-text parsing.
5. **Durable by default.** State is checkpointed after every node transition. Any run can be resumed.
6. **Bounded by default.** Loops, retries, timeouts, and cost budgets always have limits, and defaults exist even when the user doesn't set them.
7. **Local-first, no daemon required** for v1. Scheduling delegates to cron, systemd timers, or CI.
8. **Transparent.** Every run has a readable event log, and every agent session is traceable to its harness session ID.
9. **Small core, extensible nodes.** New node types and harness adapters should not require changes to the runner.

---

## 6. Core concepts (draft)

| Concept | Definition |
|---|---|
| **Flow** | A named graph definition stored in a file. Has inputs, nodes, edges, and defaults. |
| **Node** | One step. Has a type, configuration, outputs, and outgoing edges. |
| **Edge** | A transition from one node to another, optionally guarded by a condition. |
| **Run** | One execution of a flow with specific inputs. Has an ID, status, state, and event log. |
| **State** | Data accumulated during a run: inputs, each node's outputs, visit counts, and variables. |
| **Checkpoint** | Persisted state after a node transition, sufficient to resume. |
| **Adapter** | A module that drives a specific harness (Claude Code, Codex) through a common interface. |
| **Artifact** | A file or output produced by a node that later nodes or the user may need. |

### Run statuses (draft)

`pending → running → (waiting_for_human) → running → succeeded | failed | cancelled`

---

## 7. Node catalog (draft, to be finalized in spec)

**MVP nodes**

| Node | Purpose | Key config | Outputs |
|---|---|---|---|
| `agent` | Run a harness session with a prompt. | harness, model, prompt (templated), working dir, skills/agents/instructions, allowed tools/permissions, output schema, timeout, budget, resume-from session | structured output, raw text, session ID, cost/usage, exit status |
| `shell` | Run a command. | command, cwd, env, timeout | exit code, stdout, stderr |
| `condition` | Branch on an expression. | cases (expression → target), default | chosen branch |
| `human` | Pause for input or approval. | message, choices or free-text, timeout, default | choice or text, responder |
| `sleep` | Wait. | duration or until-time | none |

**Later nodes (explore in research/spec)**

- `handoff`: drop the user into an **interactive** harness session (e.g., resume the agent's session interactively), then continue the flow when they exit.
- `parallel` / `join`: fan out branches and wait for all or any of them.
- `map`: run a subgraph per item in a list.
- `subflow`: call another flow file.
- `notify`: send a desktop notification, webhook, or Slack message.
- `git`: create a worktree or branch, commit, or open a PR.
- `http`: make a request.
- `wait_for`: poll until a condition holds (CI finished, file exists).
- `set`: assign or transform state variables.

---

## 8. Illustrative flow file (not final syntax)

```yaml
name: implement-feature
description: Plan with Claude, implement with Codex, loop on tests, ask before merging.

inputs:
  feature: { type: string, required: true }

defaults:
  timeout: 30m
  max_visits: 5          # per node, prevents infinite loops
  budget_usd: 10

nodes:
  plan:
    type: agent
    harness: claude
    prompt: |
      Write an implementation plan for: {{ inputs.feature }}
    output_schema: schemas/plan.json
    next: implement

  implement:
    type: agent
    harness: codex
    prompt: |
      Implement this plan. Previous test failures, if any:
      {{ nodes.test.stderr | default("none") }}
      Plan: {{ nodes.plan.output | json }}
    next: test

  test:
    type: shell
    run: npm test
    next:
      - when: "nodes.test.exit_code == 0"
        to: approve
      - when: "visits.implement < 3"
        to: implement
      - to: escalate

  approve:
    type: human
    message: "Tests pass. Merge?"
    choices: [merge, reject]
    next:
      - when: "nodes.approve.choice == 'merge'"
        to: merge
      - to: end

  merge:
    type: shell
    run: git merge --no-ff feature-branch

  escalate:
    type: human
    message: "Tests still failing after 3 attempts. Take a look."
```

Open questions this example raises: YAML vs TOML vs other; template syntax; expression language (CEL? JMESPath? a minimal custom one?); how `next` is expressed; how outputs are addressed.

---

## 9. Architecture sketch (draft)

```
┌──────────────┐     ┌──────────────┐
│     TUI      │     │   Scripts    │
│  (client)    │     │  (bash, CI)  │
└──────┬───────┘     └──────┬───────┘
       │   CLI commands / --json   │
       └────────────┬──────────────┘
             ┌──────▼──────┐
             │   Engine    │  parse → validate → run → checkpoint
             │  (runner)   │
             └──┬───────┬──┘
     ┌──────────▼┐     ┌▼────────────┐
     │ Node types │     │ Store       │  .arcflow/runs/<id>/
     │ (registry) │     │ (state,     │    state.json
     └─────┬──────┘     │  events)    │    events.jsonl
           │            └─────────────┘    nodes/<node>/…
     ┌─────▼──────────────┐
     │ Harness adapters   │  claude, codex, fake (tests)
     └────────────────────┘
```

### Adapter interface (conceptual)

```
start(request) -> handle
  request: prompt, cwd, model, instructions/skills, permissions,
           output_schema, timeout, resume_session_id?
stream(handle) -> events   (progress, tool calls, text)
result(handle) -> { output, raw_text, session_id, usage, cost, status }
cancel(handle)
interactive(session_id)    (for handoff nodes, if supported)
```

The **fake adapter** returns scripted responses so the engine can be tested deterministically without real agents or cost.

### Headless entry points to research

- Claude Code: `claude -p` with JSON/stream-JSON output, session resume, and the **Claude Agent SDK**.
- Codex: `codex exec` with JSON output, and the **Codex SDK**.

---

## 10. CLI sketch (draft)

```
arcflow validate <flow>                 # check syntax, graph, schemas
arcflow graph <flow> [--mermaid]        # render graph
arcflow run <flow> [--input k=v ...]    # start a run (foreground or --detach)
arcflow resume <run-id>                 # continue from last checkpoint
arcflow status [<run-id>]               # show run state
arcflow list [--flow X] [--status Y]    # list runs
arcflow logs <run-id> [--follow]        # event log
arcflow respond <run-id> <node> --choice merge   # answer a human node
arcflow cancel <run-id>
arcflow tui                             # open TUI
```

All commands support `--json`. Exit codes are documented and stable.

## 11. TUI sketch (draft)

- **Flows view:** flows in the repo, with validate and run actions.
- **Runs view:** active and past runs with status, duration, and cost.
- **Run detail:** graph with live node status, per-node logs and outputs, and pending human prompts to answer inline.
- **Later:** graph editing. For v1, editing happens in the user's own editor.

---

## 12. Open decisions

Record each resolved decision as an ADR in `docs/adr/`.

> **Resolved 2026-09-30:** all ten are decided in ADRs 0001–0010 (`docs/adr/README.md`); ADRs 0011–0012 add the adapter architecture and run storage. The ADRs override the options listed below.

1. **Implementation language:** Go (Bubble Tea), Rust (Ratatui), or TypeScript (Ink).
2. **Flow file format** and template/expression language.
3. **State and addressing model:** how nodes read other nodes' outputs.
4. **Workspace isolation:** do agent nodes share a working dir, or does each get a git worktree?
5. **Permissions model:** how much autonomy agent nodes get, and how it's configured.
6. **Human node delivery:** only CLI/TUI, or also notifications?
7. **Parallelism in v1:** yes or no?
8. **Scheduling:** external only (cron/CI), or a built-in lightweight scheduler later?
9. **Name.**
10. **License** (MIT likely).

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Vendors ship similar features | Focus on cross-harness, local, and file-based. |
| Harness CLIs change often | Thin adapters, contract tests, pinned versions. |
| Unbounded cost or infinite loops | Mandatory limits and budgets with defaults. |
| Unsafe autonomous actions | Explicit permissions per node, and human nodes before risky steps. |
| TUI graph editing is hard | Defer it; text file first. |
| Scope creep | Non-goals section in the spec; milestones with acceptance criteria. |

## 14. Non-goals (v1)

- Calling model APIs directly or implementing an agent loop.
- A web UI or hosted service.
- A long-running daemon or built-in distributed execution.
- Free-form visual layout in the TUI (structured editing of the flow file is in v1, ADR 0014).
- Multi-user collaboration.

---

## 15. Project plan

### Stage 1: Research
Produce `docs/research.md` with findings, recommendations, and decisions for §12.
- Harness headless interfaces: structured output, session resume, permissions, streaming, cost reporting, cancellation, interactive handoff. **Highest priority.**
- Deep-dive prior art: Claude Code dynamic workflows, lionagi, Open Dynamic Workflows, LangGraph (state and checkpointing), Temporal (durable execution), GitHub Actions (YAML ergonomics).
- Flow format and expression language options.
- TUI framework comparison: graph rendering, live logs, distribution.

### Stage 2: Spec
Produce `docs/spec.md`, the authoritative definition. Sections: concepts, file format with 3–4 complete example flows, node catalog (inputs, outputs, failure behavior), execution semantics (data passing, retries, loop limits, timeouts, budgets), persistence and resume, adapter interface, CLI and TUI, security, testing strategy, non-goals.

### Stage 3: Build phases
Produce `docs/milestones.md`. Each milestone is sized for roughly one agent session, with acceptance criteria and required tests. The table below is the original outline; `docs/milestones.md` is authoritative. It keeps these numbers, splits large milestones into lettered parts, and adds the features ADR 0014 brings into v1 (structured editing, the remaining node types, command adapters).

| # | Milestone | Done when |
|---|---|---|
| M0 | Repo skeleton, CI, lint, `CLAUDE.md`/`AGENTS.md` | CI green on an empty project |
| M1 | Flow parsing and validation | `arcflow validate` catches the invalid-flow test suite |
| M2 | Runner with shell, sleep, and condition nodes, plus checkpoints | Example flows run; a killed run resumes correctly |
| M3 | Fake adapter, then Claude and Codex adapters | Engine tests pass with fake; smoke tests pass with real harnesses |
| M4 | Human node, pause, and `respond` | Flow pauses, is answered via CLI, and continues |
| M5 | Full CLI with `--json` | All §10 commands work and are documented |
| M6 | Read-only TUI monitor | Live view of a running flow |
| M7 | TUI run control and human prompts | Run, cancel, and respond from TUI |
| M8 | Docs, examples, first release | Installable binary; README quickstart works |

### Stage 4: Build
Per milestone: plan (Claude Code plan mode) → implement against spec, tests first → independent review in a fresh session or a different harness → merge → update docs. After M4, use Arcflow to orchestrate its own build loop (dogfooding).

---

## 16. Working agreements for agents

1. **The spec is the source of truth.** If the spec is ambiguous or wrong, stop and raise it; don't silently invent behavior.
2. **Stay in the current milestone.** No features from later milestones or non-goals.
3. **Tests first.** Engine tests use the fake adapter. Never call real harnesses or spend money in unit tests.
4. **Record decisions.** Any non-trivial choice gets a short ADR in `docs/adr/NNNN-title.md`.
5. **Keep docs in sync.** Behavior changes update the spec and README in the same change.
6. **Small, reviewable changes** with clear commit messages.
7. **Verify external facts.** Harness CLIs and SDKs change; check current docs rather than relying on memory.

---

## 17. Glossary

- **Harness:** a complete agent application (Claude Code, Codex CLI) that manages its own loop, tools, and context. Arcflow drives harnesses; it isn't one.
- **Headless mode:** running a harness non-interactively from a script with machine-readable output.
- **Human-in-the-loop:** a point where a flow pauses for a person to approve, choose, or take over.
- **Durable execution:** persisting progress so work survives crashes and can resume.
- **Worktree:** a git feature for multiple working directories on separate branches of one repo; useful for isolating parallel agents.
