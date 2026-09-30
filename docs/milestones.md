# Floxim Milestones

| | |
|---|---|
| Status | Draft 1 for review by Alexey |
| Date | 2026-09-29 |
| Stage | 3 (Build phases), per PROJECT_BRIEF.md §15 |
| Inputs | `docs/spec.md` (accepted), ADRs 0001–0014 |

This is the build plan for Floxim v1. It orders the work into milestones, each sized for roughly one agent session, with the spec sections it implements, what it depends on, its acceptance criteria and the tests it must add. The spec defines behaviour; this document only schedules it. If a milestone seems to need behaviour the spec doesn't define, stop and raise it (brief §16.1).

## How to read this

**Numbering.** The brief's milestones M0–M8 keep their numbers and meanings, because the spec, research and ADRs refer to them (M1 validation, M3 adapter contract tests, M4 human nodes, M8 first release). A milestone too large for one session is split into lettered parts (M1a, M1b, …) that are done in order. Features added to v1 by ADR 0014 are lettered milestones placed after the milestone they depend on.

**Scope of v1** (ADR 0014): the spec's Core tier, which includes structured editing, all eleven node types, `include`, command adapters and the conformance kit. Agent authoring (spec §11), AAP (§8.6) and ACP (§8.7) are Planned and not scheduled here.

**Per-milestone workflow** (brief §15, Stage 4): plan in plan mode → write the tests → implement against the spec → independent review in a fresh session or a different harness → merge → update docs.

## Definition of done (every milestone)

A milestone is done only when all of these hold, in addition to its own criteria:

1. CI is green: lint, tests on Python 3.10–3.13 on Linux and macOS, and the pure-Python dependency check (ADR 0001).
2. Tests were written first, at the layer spec §13 names for the work. Unit tests never call a real harness or spend money; `live` tests are opt-in and excluded by default.
3. Every validation code the milestone introduces has at least one file in `tests/flows/invalid/` with its `# expect:` annotation (code and position).
4. Every event type the milestone emits matches Appendix B and appears in at least one golden event log.
5. Every CLI command or flag the milestone adds has a `--json` snapshot test and tests for its exit codes (§9.2).
6. Every node type the milestone makes runnable joins the crash-and-resume matrix (§13): crash at each event boundary, resume, and assert the same final state with no finished visit run twice.
7. Behaviour the milestone had to clarify is written back into the spec in the same change; new non-trivial decisions get an ADR.

**`E-NOT-IMPLEMENTED` during the build.** One table in the code lists the node types and features the runner can execute, and each milestone that adds one updates it. `floxim validate` checks the format and semantics of every feature from M1 on, and reports `E-NOT-IMPLEMENTED` as its last stage for anything outside that table. Corpus tests assert the other stages separately, so the Appendix A flows are in the valid corpus from M1 even before they can run.

## Overview

| Milestone | Title | Depends on |
|---|---|---|
| M0 | Repo skeleton, CI, packaging | — |
| M1a | Loader, flow model and `validate` | M0 |
| M1b | Expressions and templates | M0 |
| M1c | Graph, reference and security checks | M1a, M1b |
| M2a | Run store: event log, state, lock, inbox | M1a |
| M2b | Step loop with `condition`, `sleep` and `set` | M1c, M2a |
| M2c | `shell` node and process control | M2b |
| M2d | Resume and crash safety | M2c |
| M2e | Workspaces and artifacts | M2c |
| M2f | `python` node | M2c |
| M3a | Adapter contract, fake adapter, `agent` node | M2d |
| M3b | Structured output, budgets and cost | M3a |
| M3c | Claude adapter | M3b |
| M3d | Codex adapter | M3b |
| M3e | Command adapters and conformance kit | M3c, M3d |
| M4a | `human` node, waiting, `respond`, detached runners | M2d |
| M4b | `notify`, `cancel`, human timeouts, `on_resume: ask` | M4a |
| M4c | `subflow` and `map` | M4b |
| M4d | `handoff` | M3c, M3d, M4a |
| M5a | Inspection commands | M4b |
| M5b | Housekeeping commands and the CLI contract | M5a, M3e |
| M5c | Structured editing library and `floxim flow` | M1c |
| M6a | Graph renderer | M1c |
| M6b | Read-only TUI monitor | M5a, M6a |
| M7a | TUI run control and human prompts | M6b |
| M7b | TUI graph editor | M7a, M5c |
| M8 | Docs, examples, first release | all |

Dogfooding starts after M4b (brief §15): Floxim's own milestone loop (plan → implement → test → review → approve) runs as an Floxim flow from then on.

M1b, M2e, M2f, M5c and M6a are off the critical path, and their order among the others is flexible.

---

## M0. Repo skeleton, CI, packaging

**Implements:** ADR 0001, ADR 0010, ADR 0013; spec §13 (packaging row).

**Scope**

- `pyproject.toml` with a pure-Python build backend; package `floxim` under `src/`; console scripts `floxim` and `flx` for the same entry point; `floxim --version`.
- Top-level error handling: an unexpected exception exits with code 70 and asks for a bug report (§9.2).
- Dev tooling: pytest, Hypothesis, a linter and formatter, and a type checker.
- CI (GitHub Actions): lint, type check, tests on 3.10–3.13 × Linux and macOS, wheel and zipapp build, and a check that every transitive runtime dependency installs as a `py3-none-any` wheel.
- `LICENSE` (MIT), `AGENTS.md` (pointing at `CLAUDE.md`'s rules for non-Claude harnesses), a stub `README.md`.
- An ADR choosing the CLI library (`typer` or `argparse`, ADR 0001 left it open), the build backend and the type checker.

**Done when**

- CI is green on a project with no features.
- `pipx install .` and `uv tool install .` both give working `floxim --version` and `flx --version`; so does the zipapp run with a system Python 3.10.
- The pure-Python check fails when a native dependency is added (proved by the check's own test against a fixture list of wheels).

## M1a. Loader, flow model and `validate`

**Implements:** §3.1–3.8 (all keys of every node type in §5, including `include`), §9.4 stages 1–5, §9.1 `--json` envelope, Appendix C codes `E-YAML`, `E-YAML-ALIAS`, `E-DUPLICATE-KEY`, `E-SCHEMA`, `E-UNKNOWN-KEY`, `E-VERSION`, `E-BAD-ID`, `E-RESERVED-ID`, `E-UNKNOWN-TEMPLATE`, `E-TEMPLATE-CYCLE`, `E-TEMPLATE-ROUTING`, `E-MUTUALLY-EXCLUSIVE`, `E-FILE-NOT-FOUND`, `E-BAD-JSON-SCHEMA`.

**Scope**

- `ruamel.yaml` loading in YAML 1.2 core mode with line/column positions for every value; rejection of anchors, aliases, merge keys and duplicate keys.
- A declarative model of flows, inputs, defaults, limits, templates and all eleven node types, from which both the validator and the published schema derive; `x-*` keys preserved and ignored; durations and identifiers.
- Effective-node computation: built-in defaults → `defaults` → `defaults.<type>` → templates (`extends` chains) → node keys, with the merge rules of §3.6; `include` of template-only fragments.
- Referenced files resolved relative to the flow file; JSON Schemas checked with `fastjsonschema`.
- `floxim validate <flow>… [--strict] [--json]`, printing problems as `file:line:col severity CODE pointer: message` and as the JSON array of §9.4. The `--json` envelope and the exit-code constants are shared code from here on.
- `floxim schema flow`, generated from the same dataclasses.
- The corpus runner: `tests/flows/invalid/*.yaml` with `# expect:` annotations, `tests/flows/valid/*.yaml` with all four Appendix A flows.

**Done when**

- The invalid corpus catches every code listed above, at the annotated positions.
- The Appendix A flows load and produce the expected effective nodes (golden files).
- The published schema validates every valid-corpus flow and rejects a sample of invalid ones (the schema and the parser agree).

## M1b. Expressions and templates

**Implements:** §4.1, §4.2, §4.4; codes `E-EXPR-SYNTAX`, `E-EXPR-FORBIDDEN`, `E-EXPR-UNKNOWN-FUNCTION`, `W-JINJA-LIKE`.

**Scope**

- `ast`-whitelist parser and evaluator: allowed syntax, rejected constructs, `true/false/null` aliases, null-safe attribute and key access, JSON-type semantics, the 4,000-character limit.
- Every function of §4.2, with argument checking and run-time errors that carry an `expression_error` kind.
- Template rendering: whole-value rule, interpolation rule, `$${{` escaping, rendering of `None`, booleans, numbers, lists and objects.
- A pure library with no dependency on flows or runs; the state namespace is passed in as a mapping.

**Done when**

- Table-driven tests cover every allowed construct, every forbidden one and every function.
- Hypothesis property tests show no accepted expression reaches builtins, dunders, imports or method calls.
- Evaluation of a typical `when:` stays in the tens of microseconds (a benchmark test with a loose bound, to catch regressions, not to tune).

## M1c. Graph, reference and security checks

**Implements:** §3.9, §4.5, §9.4 stages 6–7 and the security lints, §12.2, §12.5, §12.7, §2.1–2.2 (project root, config loading and its schema); codes `E-UNKNOWN-TARGET`, `E-UNKNOWN-START`, `E-DEFAULT-NOT-LAST`, `E-MULTIPLE-DEFAULTS`, `E-CONDITION-NEEDS-DEFAULT`, `E-NO-EXIT`, `E-UNKNOWN-REF`, `E-WORKSPACE-NO-GIT`, `W-NO-DEFAULT-ROUTE`, `W-UNREACHABLE`, `W-UNGUARDED-CYCLE`, `W-ERROR-NEVER-ROUTED`, `W-NEVER-SET`, `W-UNBOUNDED`, `W-SHELL-INTERPOLATION`, `W-FULL-PERMISSIONS`, `W-NO-HUMAN-BEFORE-RISKY`, `I-UNCHECKED-OUTPUT`.

**Scope**

- Graph model (nodes, edges with `when`, `on_error` edges) used by validation now and by `graph`, the renderer and the editor later.
- Routing checks, reachability, exits, unguarded cycles.
- Reference resolution for every expression and template: inputs, node IDs, per-type result fields, `output` paths through the node's `output_schema` (`properties`, `items`, local `$ref`), `W-NEVER-SET` from path analysis.
- Security lints, with the risky-command patterns taken from config.
- Project-root discovery and config loading with precedence (§2.2); `floxim schema config`.
- Did-you-mean hints for unknown targets and references.

**Done when**

- The invalid corpus covers every code above.
- The valid corpus, including all Appendix A flows, reports no errors other than `E-NOT-IMPLEMENTED`, and its expected warnings are pinned in the corpus annotations (A.2's include the `W-NO-HUMAN-BEFORE-RISKY` that §A.2 predicts).
- **Brief M1 is complete:** `floxim validate` catches the whole invalid-flow suite.

## M2a. Run store: event log, state, lock, inbox

**Implements:** §7.1–7.4, §1.1, ADR 0012; Appendix B envelope.

**Scope**

- Run IDs and run-argument resolution (`@last`, `@last:<flow>`, unique prefix or suffix).
- Run directory layout, `run.json`, flow snapshot (the flow file and every file it references), `0700` permissions.
- Event writer: sequence numbers, `fsync` on checkpoint events, flush-only for progress events; reader tolerant of a torn final line and strict about everything else.
- State reducer: `state.json` derived from events, written atomically, rebuilt when `last_seq` disagrees.
- Runner lock with heartbeat and staleness rules; inbox with atomic request files.
- A library only, with no CLI.

**Done when**

- Property tests show the reducer's state is the same whether built incrementally or replayed from the log.
- A torn final line is ignored with a warning; a corrupt middle line or a `seq` gap is reported as corruption.
- Two processes racing for a lock never both hold it; a lock from a dead PID or with an old heartbeat is taken over and recorded.

## M2b. Step loop with `condition`, `sleep` and `set`

**Implements:** §6.1–6.5, §6.3 size limits, §5.3, §5.5, §5.6, §3.4 limits, §4.3 state namespace; `floxim run` (foreground, `--input`, `--inputs-file`, `--input k=@file`, `--workdir`, `--json`, `--events`); exit codes 0, 1, 3.

**Scope**

- Run lifecycle (create, start, step loop, finish with `outputs`), input coercion and required-input errors.
- Visit and attempt bookkeeping, template rendering at visit start, `visit.json`, routing via `next` and `on_error`, `route_taken` with reasons, `to: fail` messages.
- Limits: `max_visits`, `max_steps`, `max_duration` (active time).
- `condition`, `sleep` (`duration`/`until`, `wake_at`) and `set` nodes.
- An injected clock, so tests of sleeps and durations run instantly.
- The one-line notice for flow files not tracked by git (§12.1).

**Done when**

- Golden event logs for flows exercising every routing form, every limit and each of the three node types.
- A loop without a guard stops at `max_visits` and names the node.
- `floxim run` on a condition/sleep/set-only flow succeeds and exits 0, fails and exits 1, and rejects bad inputs with exit 3.

## M2c. `shell` node and process control

**Implements:** §5.2, §6.6, §6.7, §12.3, §12.4.

**Scope**

- The process layer, one module for all child processes: process groups, the SIGINT → grace → SIGTERM → SIGKILL stop sequence with `stopped_by`, per-attempt timeouts. Platform-specific code is isolated there (§15 Q10).
- `shell` node: `run` through `bash -eo pipefail` (or `sh -e`), `args` without a shell, `cwd`, `stdin`, `ok_codes`, `output: json|text`, `output_schema`, output tails and full logs, `FLOXIM_*` variables.
- Environment control: empty environment plus the allowlists, `env_passthrough`, node `env`; the session denylist always applied; `env.X` in templates limited to allowed variables.
- Retries with backoff and `on` outcomes.
- Redaction of everything Floxim writes (§12.4).

**Done when**

- Timeouts stop a process tree that ignores SIGINT, within the configured grace period plus 5 s.
- A variable set in the runner's environment but not allowed never reaches the child (tested for each allowlist source and for each denylisted name).
- A fix-loop flow in the valid corpus (shell steps, a failing test step with `on_error: continue`, a `visits` guard) runs end to end and takes each branch.
- **Brief M2's first half:** example flows made of shell, sleep and condition nodes run.

## M2d. Resume and crash safety

**Implements:** §7.5 (`resume` and `restart` modes), §7.6 (`--reload`, `--from`, `--rerun`, `--force`), §6.7 runner signals, §13 crash-and-resume layer; `floxim resume`; exit code 8.

**Scope**

- Rebuilding state from events and continuing; interrupted visits handled by `on_resume` (`ask` comes in M4b).
- The resume options above, including snapshot versioning for `--reload`.
- SIGINT/SIGTERM to a foreground run: stop the attempt, record `runner_detached`, exit 8; a second SIGINT within 3 s skips the grace period.
- The fault-injection hook `FLOXIM_TEST_CRASH_AT=<event type>:<n>` and the crash matrix harness.
- The derived `interrupted` display status.

**Done when**

- For every runnable valid-corpus flow, crashing at every event boundary and resuming gives the same final state as an uncrashed run, and no finished visit runs twice.
- **Brief M2 is complete:** a killed run resumes correctly.

## M2e. Workspaces and artifacts

**Implements:** §6.9, §6.10; `E-WORKSPACE-MISSING`, `--recreate-workspaces`.

**Scope**

- `workspace: worktree` and named worktrees, created on first use with the recorded base and branch, reused across visits and nodes, reused on resume.
- `keep` handling (removal happens in `floxim gc`, M5b).
- Artifact directories per visit, `node.artifacts_dir`, `FLOXIM_ARTIFACTS_DIR`, `nodes.<id>.artifacts_dir`.

**Done when**

- Tests in temporary git repositories show two nodes naming the same worktree see each other's changes, and a `shared` node sees `workspace.path` and `workspace.branch`.
- Resume reuses a recorded worktree, and fails with `E-WORKSPACE-MISSING` when it has been deleted, unless `--recreate-workspaces`.

## M2f. `python` node

**Implements:** §5.7.

**Scope**

- `floxim.pycall` child entry point, `interpreter:`, project root on `sys.path`, `args` as keyword arguments and a read-only `ctx`, JSON return values, `output_schema`.
- Exceptions become outcome `failed` with kind `exception` and the traceback in the visit directory.
- Timeouts and cancellation through the M2c process layer.

**Done when**

- A function that hangs is stopped by `timeout`; a function that raises is routed by `on_error`; a non-JSON return value fails with a clear message.
- The runner process never imports the called module (asserted by the test).

## M3a. Adapter contract, fake adapter, `agent` node

**Implements:** §8.1 (contract, capabilities, degradation), §8.3 `fake`, §8.6 entry-point plugins, §5.1 (keys, result fields, §5.1.1 success, §5.1.3 sessions), §7.5 `on_resume: resume` for agents, §12.2 `--allow-full`; codes `E-UNKNOWN-HARNESS`, `E-HARNESS-OPTIONS`, `E-PERMISSION-UNSUPPORTED`, `E-SESSION-HARNESS`, `W-IGNORED-OPTION`, `I-EXTRA-ARGS`.

**Scope**

- The `Adapter` protocol and its data types; the adapter registry (built-ins plus lazily loaded `floxim.adapters` entry points).
- `ProcessAdapter` base on the M2c process layer.
- The fake adapter: scripted responses by order or `match`, outcomes and errors, delays, usage and cost, and (as a stub until M3c) stream replay.
- The `agent` node in the engine: rendered `prompt.md`/`instructions.md`, `session_started` written and `fsync`ed first, session modes with graceful degradation, `permission_denials`, results in state.
- The validator checks that need adapters: harness names, `harness_options` schemas, capability and permission checks, session harness checks, and the `permissions: full` refusal at run time.

**Done when**

- Engine tests drive every session mode, every outcome and every degradation row of §8.1 through the fake adapter.
- An agent visit crashed after `session_started` resumes that session on `floxim resume`.

## M3b. Structured output, budgets and cost

**Implements:** §5.1.2, §6.8, §2.2 `prices`; `W-USD-UNENFORCEABLE`.

**Scope**

- Native and prompt-based structured output, JSON extraction from text, re-validation with `fastjsonschema`, `schema_retries` by resuming the session or re-prompting, `schema_invalid`.
- Run and node budgets in USD and tokens: accounting after every attempt, `budget_updated`, the check before each agent visit, remaining allowance passed to the adapter, streamed-usage monitoring that stops an attempt that crosses a budget.
- Cost from the harness, else from `prices`, else unknown; `cost_estimated`; `~$` display.

**Done when**

- Fake-adapter tests cover every path above, including a budget crossed mid-attempt and a schema fixed on the second try.
- A plan → implement → test loop (Example A.1 without its human nodes) runs end to end with the fake adapter and produces the expected event log.
- **Brief M3's first criterion:** engine tests pass with the fake adapter.

## M3c. Claude adapter

**Implements:** §8.3 `claude`, §8.4 Claude column, §12.3 Claude auth and denylist.

**Scope**

- Command construction, prompt on stdin, `--session-id` chosen by Floxim, `stream-json` parsing into adapter events, the raw stream saved byte for byte, outcome mapping from `subtype`/`terminal_reason`, version capture and `tested_versions` warning, `--disallowedTools Workflow`, `interactive_command`.
- Offline fixtures recorded from the pinned version under `fixtures/claude-<version>/`, replayed through the parser (also by the fake adapter's `replay`).
- **Contract tests** (marked `live`, opt-in, with a spend cap): settle the exact flags per permission profile (§15 Q6) and the env denylist (§15 Q7), then update §8.4 and §12.3 from the results.

**Done when**

- Offline fixtures cover success, structured output, schema failure, budget stop, SIGINT with a result, SIGTERM without one, auth failure and a missing result.
- Live smoke tests pass against the pinned Claude Code version: session ID, resume, fork, schema output, cancel within the grace period, env isolation, and each permission profile's intent (read-only cannot write, edit cannot run unlisted commands).

## M3d. Codex adapter

**Implements:** §8.3 `codex`, §8.4 Codex column, §12.3 Codex auth and denylist; `W-CODEX-STRICT-SCHEMA`.

**Scope**

- `codex exec --json` construction, `exec resume` and `exec fork`, `thread.started` session capture, event parsing, `last-message.txt`, `harness_options` (`network`, `profile`, `ignore_user_config`, `extra_args`), `interactive_command`.
- Offline fixtures under `fixtures/codex-<version>/`.
- **Contract tests** (`live`): settle Codex's signal handling, exit codes, default sandbox and schema subset (§15 Q8, research §7), then update §8.3 and `W-CODEX-STRICT-SCHEMA` from the results.

**Done when**

- Offline fixtures cover the same cases as M3c.
- Live smoke tests pass against the pinned Codex version.
- **Brief M3 is complete:** smoke tests pass with both real harnesses.

## M3e. Command adapters and conformance kit

**Implements:** §8.5, §8.8, §9.3 `floxim adapter test`, `floxim schema harness`.

**Scope**

- `.floxim/harnesses/<name>.yaml` loading and validation, per-argv placeholder substitution with the flag-pair omission rule, `prompt_via`, stream mappings evaluated with the M1b evaluator, permission argv, auth env, `tested_versions`.
- The conformance suite: offline mode replays fixture streams through a built-in adapter's parser, or through a stub executable for command adapters; live mode (`--live`) runs the small prompt set with a spend cap.
- A sample command adapter in the tests (driving a stub CLI) that passes the offline suite.

**Done when**

- `floxim adapter test claude`, `codex` and the sample command adapter pass offline.
- A malformed harness file produces precise validation problems with positions.

## M4a. `human` node, waiting, `respond`, detached runners

**Implements:** §5.4, §6.11 (`--on-wait prompt|wait|exit`), §7.4 inbox for answers, ADR 0006; `floxim respond`, `floxim run --detach`; exit code 4; exit code 2 for invalid answers.

**Scope**

- `human` node: choices, text, acknowledgement, `show`, `human_waiting`, `run_waiting`, the answer recorded as `human_responded`.
- `on_wait` hooks from node, flow or config, run detached with a 30 s timeout and `FLOXIM_*` variables (the hook runner is reused by `notify` in M4b).
- The three `--on-wait` behaviours, including the inline terminal prompt that also accepts answers from other clients.
- `floxim respond` through the inbox when a runner is live, otherwise by taking the lock, then starting a detached runner unless `--no-continue`.
- `floxim run --detach`.

**Done when**

- A flow started with `--on-wait exit` exits 4 at a human node; `floxim respond` from another process records the answer, starts a detached runner, and the run finishes.
- An invalid answer exits 2 and changes nothing.
- Example A.1 runs end to end with the fake adapter, with its approval answered through `floxim respond`.
- **Brief M4 is complete:** a flow pauses, is answered through the CLI, and continues.

## M4b. `notify`, `cancel`, human timeouts, `on_resume: ask`

**Implements:** §5.11, §6.7 cancel, §6.11 lazy timeouts, §7.5 `ask`, §7.6 `--due`; `floxim cancel`; exit code 5.

**Scope**

- `notify` node with `command` or `webhook` and `required`.
- `floxim cancel` through the inbox or directly; a waiting run is cancelled at once.
- Human timeouts: `default` answers, `timed_out` outcomes, lazy enforcement by any command that touches the run, `floxim resume --due`.
- `on_resume: ask` as a generated human prompt with `rerun`, `skip` and `fail`.

**Done when**

- A running agent (fake, slow) and a running shell command are both cancelled within the grace period, and the run ends `cancelled` with exit 5.
- With the injected clock, a human node past its deadline times out when `floxim status` or `resume --due` touches the run.
- The dogfooding flow for Floxim's own milestones validates and runs with the fake adapter.

## M4c. `subflow` and `map`

**Implements:** §5.8, §5.9, §7.5 resume of child runs.

**Scope**

- Child runs linked by `parent` and `child_run`, limits and remaining budget passed down, outputs returned, recursion limit of 8.
- Cancelling a parent cancels its child; a human node in a child makes the parent `waiting`, and `respond` on the parent forwards to the child.
- `map`: sequential child runs per item, `item` and `index`, `max_items`, `on_item_error`, results and counts, `concurrency` fixed at 1.
- Resume continues an unfinished child run, and a `map` continues with the next unfinished item.

**Done when**

- Example A.4 runs end to end with the fake adapter and a stub `gh` on `PATH`.
- The crash matrix covers a crash inside a child run and between map items.

## M4d. `handoff`

**Implements:** §5.10, `floxim handoff`.

**Scope**

- In the foreground on a TTY: print the message, run the adapter's `interactive_command` attached to the terminal, continue on exit.
- Otherwise the run waits like a human node, and `floxim handoff <run>` opens the session later and continues the run.
- Validation: the `from` node exists, is an agent node, and its adapter declares `interactive`.

**Done when**

- Tests using a pseudo-terminal and a stub interactive command cover both paths.
- A manual check with Claude Code and Codex is recorded in the milestone's PR (a real interactive session can't run in CI).

## M5a. Inspection commands

**Implements:** §9.3 `status`, `list`, `logs` (`--node`, `--visit`, `--follow`, `--raw`, `--prompt`), `wait`, `artifacts`, `flows`, `graph --format json|mermaid|dot`, derived `interrupted` status in listings.

**Done when**

- Every command has `--json` snapshot tests and exit-code tests against run directories produced by fake-adapter runs.
- `logs --follow --events` on a live run streams events as they are written and ends when the run does.

## M5b. Housekeeping commands and the CLI contract

**Implements:** §9.1 (global flags, `--quiet`, `--verbose`, `--no-color`, `--as`), §9.3 `init`, `schema cli|config|harness`, `adapters [--probe]`, `gc`, `doctor [<run>]`, §7.7.

**Scope**

- `floxim init` creates `.floxim/` with config, `.gitignore` and an example flow that validates.
- `floxim schema cli` publishes the JSON Schema of every command's `--json` output; snapshot tests validate against it.
- `gc` with its safety rules (never waiting or locked runs; worktrees by `keep` or merged branch); `doctor` for the environment and for repairing a run's log and state.

**Done when**

- The published CLI schemas validate every command's `--json` output in the test suite.
- **Brief M5 is complete:** every command marked Core in §9.3 (except `tui`) works, has `--json`, and has documented exit codes.

## M5c. Structured editing library and `floxim flow`

**Implements:** §10.4 (all operations), `floxim flow add-node|rm-node|rename-node|set|unset|connect|disconnect`; exit code 7 on conflict.

**Scope**

- `floxim.edit` on the `ruamel.yaml` round-trip tree: every operation in §10.4, validation of the result, re-read and hash check before writing, atomic write keeping the file mode.
- Renaming rewrites every reference, including inside expressions through the AST, and leaves text outside `${{ }}` untouched.

**Done when**

- Round-trip tests on a corpus of hand-formatted flows: no operation gives a byte-identical file; each operation's diff touches only the expected lines.
- An edit that would make a valid file invalid is refused; a file changed on disk between read and write exits 7 and is left untouched.

## M6a. Graph renderer

**Implements:** §10.2; `floxim graph --format ascii`.

**Scope**

- Layered layout with `grandalf`, box-drawing output, back edges routed on the side, shortened `when` labels, node status styling hooks for the TUI, the vertical-list fallback above 60 nodes or 200 ms.

**Done when**

- Golden text renderings for every valid-corpus flow, including loops.
- The fallback triggers at the thresholds.

## M6b. Read-only TUI monitor

**Implements:** §10.1 Flows and Runs screens and Run detail (viewing), §10.3 live file sync, §10.5.

**Scope**

- Textual app launched by `floxim tui [<flow>|<run>]`; screens read only through the library functions the CLI uses.
- Run detail: live node status on the graph, visit timeline, inspector (prompt, output, streamed activity, stdout/stderr, artifacts, usage and cost), budget and limit gauges, pending-prompt banner (display only in this milestone).
- File polling every 300 ms with the valid and invalid behaviours of §10.3.
- Key bindings, command palette, monochrome fallback, 80×24 minimum.

**Done when**

- Pilot and SVG snapshot tests against fake-adapter run directories, including a run that changes while the screen is open.
- **Brief M6 is complete:** a running flow can be watched live.

## M7a. TUI run control and human prompts

**Implements:** §10.1 actions: run (input form from `inputs`), cancel, resume, respond (modal and banner), open handoff, open artifact, copy session ID, open in harness.

**Done when**

- Pilot tests start, cancel, resume and answer a run, and each action produces the same events as its CLI equivalent.
- Human message rendering escapes markup and strips control sequences (§12.5).
- **Brief M7 is complete.**

## M7b. TUI graph editor

**Implements:** §10.1 Flow graph / editor screen, §10.3–10.4 from the TUI.

**Scope**

- Inspector forms generated from the node type schemas; multi-line prompt editing and `$EDITOR`; add, remove, rename, connect, disconnect, reorder cases, change type, all through `floxim.edit`.
- Conflicts: reload and ask when the file changed on disk.
- "Flow changed since this run started" in run detail, with `resume --reload` offered only when allowed.

**Done when**

- Pilot tests perform each edit and assert the resulting file matches the same `floxim flow` operation byte for byte.
- An external edit made while the editor is open appears in the graph within one polling interval.

## M8. Docs, examples, first release

**Scope**

- `README.md` quickstart; user docs for the flow format, CLI, cron/CI recipes (§6.11, ADR 0008), security (§12.1, at-least-once side effects), adapters and the conformance kit.
- `examples/` with the Appendix A flows and their schemas.
- Release pipeline: wheel and zipapp artifacts, PyPI publishing, version and changelog.
- The domain and trademark check that ADR 0017 requires before the first public release.

**Done when**

- On clean Linux and macOS machines, `pipx install floxim` from PyPI and the README quickstart work as written.
- **Brief M8 is complete.**

---

## Coverage check

Every Core feature of the spec maps to a milestone:

| Spec | Milestone |
|---|---|
| §2 layout and config | M1c (config, root), M5b (`init`) |
| §3 flow format, `include` | M1a |
| §4 expressions and templates, static checks | M1b, M1c |
| §5.1 `agent` | M3a, M3b |
| §5.2 `shell` | M2c |
| §5.3 `condition`, §5.5 `sleep`, §5.6 `set` | M2b |
| §5.4 `human` | M4a, M4b |
| §5.7 `python` | M2f |
| §5.8 `subflow`, §5.9 `map` | M4c |
| §5.10 `handoff` | M4d |
| §5.11 `notify` | M4b |
| §6.1–6.5 lifecycle, step loop, routing, limits | M2b |
| §6.6–6.7 retries, timeouts, cancellation | M2c, M4b |
| §6.8 budgets | M3b |
| §6.9–6.10 workspaces, artifacts | M2e |
| §6.11 waiting and runner processes | M4a, M4b |
| §7 persistence and resume | M2a, M2d, M4b |
| §8.1–8.4 adapters | M3a–M3d |
| §8.5 command adapters, §8.8 conformance kit | M3e |
| §8.6 entry-point plugins | M3a |
| §9 CLI | per command above; M5a, M5b |
| §10.1–10.3, 10.5 TUI | M6a, M6b, M7a |
| §10.4 structured edits | M5c, M7b |
| §12 security | M1c (lints), M2c (env, redaction), M3a (`full`), M7a (display escaping) |
| §13 testing layers | definition of done, and each milestone's tests |
