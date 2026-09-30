# Arcflow Specification

| | |
|---|---|
| Status | Accepted (reviewed by Alexey, 2026-09-29) |
| Date | 2026-09-30 (Draft 1); 2026-09-29 (review outcomes, §15) |
| Stage | 2 (Spec), per PROJECT_BRIEF.md §15 |
| Inputs | PROJECT_BRIEF.md, docs/research.md, ADRs 0001–0014 |
| Build plan | `docs/milestones.md` |
| Format version | `arcflow: 1` |

This document is the authoritative definition of Arcflow v1. Where it disagrees with the brief or research.md, this document wins; where it disagrees with an ADR, the ADR wins until the conflict is raised and resolved (see §15). Every choice the ADRs left open is made here; §15 lists the ones that were open questions in the draft and how the review resolved them.

Keywords: **must**, **must not**, **should** and **may** have their RFC 2119 meanings. The project is named Arcflow (ADR 0013): the command `arcflow` (with the short alias `arcf`), the directory `.arcflow/`, the `ARCFLOW_*` environment variables and the Python package `arcflow`.

**Tiers.** Every feature carries one of three tiers:

- **Core**: required for the v1 release, and scheduled in `docs/milestones.md` (ADR 0014).
- **Planned**: fully specified here so the format and state model leave room for it, and the validator recognizes it, but it is scheduled after v1. Until implemented, `arcflow validate` reports `E-NOT-IMPLEMENTED` for it. A Core feature whose milestone has not landed yet is reported the same way.
- **Future**: named only, to reserve the word.

## Contents

1. Concepts
2. Project layout and configuration
3. Flow file format
4. Expressions and templates
5. Node catalog
6. Execution semantics
7. Persistence and resume
8. Harness adapters
9. CLI
10. TUI, live graph and editing
11. Agent-authored flows
12. Security
13. Testing strategy
14. Non-goals
15. Open questions and defaults chosen
16. Traceability to ADRs

Appendix A: example flows. Appendix B: event reference. Appendix C: validation error codes.

---

## 1. Concepts

| Concept | Definition |
|---|---|
| **Flow** | A named, directed graph of nodes stored in **one YAML file**, the single source of truth for that flow (ADR 0002). It declares inputs, defaults, templates, nodes and outputs. Cycles are allowed and are bounded by visit limits. |
| **Node** | One step of a flow, identified by a node ID unique within the flow. It has a `type`, type-specific configuration, and outgoing routing (`next`, `on_error`). |
| **Edge** | A transition from one node to another, written in the source node's `next` list, optionally guarded by a `when:` expression. Edges exist only in `next`/`on_error`; there is no separate edge list. |
| **Run** | One execution of a flow with specific inputs. It has an ID, a status, a snapshot of the flow file, an append-only event log and a derived state. |
| **Visit** | One execution of one node within a run. A node inside a loop is visited several times; visits are numbered from 1. A visit is the unit of durability (ADR 0012). |
| **Attempt** | One try at a visit. Retries (§6.6) create further attempts within the same visit; they do not count as visits. |
| **Outcome** | The normalized result of an attempt, visit or harness call: `succeeded`, `failed`, `timed_out`, `cancelled`, `budget_exceeded`, `schema_invalid`, `interrupted` (§6.5). |
| **State** | Everything expressions can read during a run: inputs, node results, visit counts, variables and run metadata (§4.3, ADR 0003). |
| **Event** | One line of `events.jsonl`. The event log is the source of truth for a run; state is derived from it (ADR 0012). |
| **Checkpoint** | The durable point after each recorded transition. Because events are appended and flushed before the runner moves on, every event boundary is a checkpoint. |
| **Harness** | A complete agent application (Claude Code, Codex CLI) that Arcflow drives headlessly. Arcflow is not a harness and never calls model APIs itself. |
| **Adapter** | The module that drives one harness through Arcflow's adapter contract (§8, ADR 0011). |
| **Workspace** | The directory a node's process runs in: the run's shared working directory, or a Arcflow-managed git worktree (§6.9, ADR 0004). |
| **Artifact** | A file a node writes into its visit's artifact directory, for later nodes or the user (§6.10). |
| **Runner** | The process that executes a run. At most one runner holds a run at a time (§7.4). |
| **Template** | A named, reusable partial node definition inside a flow file that nodes `extends` (§3.6). |

### 1.1 Run statuses

| Status | Meaning | Terminal |
|---|---|---|
| `pending` | Created, runner not started yet. | no |
| `running` | A runner is executing it. | no |
| `waiting` | Paused at one or more human nodes (brief: `waiting_for_human`). The runner may have exited. | no |
| `succeeded` | Reached `end`. | yes |
| `failed` | Reached `fail`, an unhandled node error, a limit, or an engine error. | yes |
| `cancelled` | Cancelled by a user. | yes |

`interrupted` is a **derived display status**, not a stored one: a run whose stored status is `running` but whose runner lock is stale (§7.4) is shown as `interrupted` by `arcflow status`, `arcflow list` and the TUI, and can be resumed.

```
pending → running ⇄ waiting
             ↓
   succeeded | failed | cancelled
```

A `failed` or `cancelled` run can be resumed only with an explicit override (§7.6). A `succeeded` run cannot be resumed.

---

## 2. Project layout and configuration

### 2.1 Project root

The **project root** is the nearest ancestor of the current directory containing a `.arcflow/` directory; failing that, the root of the enclosing git repository; failing that, the current directory. `arcflow init` creates `.arcflow/` with a starter `config.yaml` and a `.gitignore`.

```
<project root>/
  flows/                      # flow files (default search path)
    implement-feature.yaml
    schemas/plan.json         # referenced by flows, relative to the flow file
    prompts/plan.md
  .arcflow/
    config.yaml               # project configuration (committed)
    harnesses/<name>.yaml     # command adapters (committed, §8.5)
    .gitignore                # ignores runs/ and worktrees/
    runs/<run-id>/            # one directory per run (§7)
    worktrees/<run-id>/<name> # Arcflow-managed worktrees (§6.9)
```

A flow file may live anywhere; `flows/` is only where discovery looks by default. All relative paths inside a flow file (`prompt_file`, `output_schema`, `subflow.flow`, …) resolve against **the flow file's directory**, never the current directory. Process working directories resolve against the **run workdir** (§6.9).

### 2.2 Configuration files

Configuration never changes what a flow means; it describes the environment the flow runs in (binaries, credentials pass-through, prices, notification hooks). Anything that changes behaviour lives in the flow file (brief principle 1).

Precedence, lowest to highest: built-in defaults → user config (`$XDG_CONFIG_HOME/arcflow/config.yaml`, default `~/.config/arcflow/config.yaml`) → project config (`.arcflow/config.yaml`) → environment variables (`ARCFLOW_*`) → command-line flags.

```yaml
# .arcflow/config.yaml
flow_paths: [flows]            # discovery roots for `arcflow list-flows` and the TUI
runs_dir: .arcflow/runs           # relative to the project root
harnesses:
  claude:
    command: claude            # binary; may be an absolute path
    tested_versions: ">=2.1.285,<2.2"   # warn outside this range
  codex:
    command: codex
env_passthrough: [AWS_PROFILE, NPM_TOKEN]   # extra variables allowed into nodes (§12.3)
prices:                        # optional; used to estimate USD where a harness reports only tokens
  codex:
    gpt-5-codex: { input_per_mtok: 1.25, cached_input_per_mtok: 0.125, output_per_mtok: 10.0 }
on_wait: 'notify-send "Arcflow" "$ARCFLOW_MESSAGE"'   # default human-node hook (§5.4); values arrive as env vars
retention: { keep_days: 30 }   # used by `arcflow gc`
redact: ['sk-[A-Za-z0-9_-]{20,}']  # regexes masked in logs Arcflow writes (§12.4)
redact_streams: true           # also redact raw harness streams (§12.4)
allow_full: false              # allow `permissions: full` without --allow-full (§12.2)
risky_commands: ['\bgit\s+push\b', '\bkubectl\b']   # regexes for W-NO-HUMAN-BEFORE-RISKY (§12.7); replaces the built-in list
```

Each harness entry also accepts `grace` (a duration, default `10s`), the stop-sequence grace period of §6.7.

The config file is validated against a published JSON Schema like flow files; unknown keys are errors.

---

## 3. Flow file format

### 3.1 General rules

- One flow per file; UTF-8; YAML 1.2 core schema (so `yes`, `no`, `on`, `off` are strings, not booleans). File extension `.yaml` or `.yml`.
- **Strict:** unknown keys are errors, except keys starting with `x-`, which are preserved and ignored (for tools and comments that need structure).
- Anchors, aliases and merge keys (`<<:`) are **rejected**. They make round-trip editing ambiguous; use `templates` instead (§3.6).
- Duplicate keys are errors.
- A **published JSON Schema** (`arcflow schema flow`) describes the file for editors (yaml-language-server) and authoring agents. The schema and this section must stay in sync; the schema is generated from the same declarations the validator checks against, so the two cannot disagree.
- **Durations** are strings of one or more `<int><unit>` groups with units `s`, `m`, `h`, `d` (`90s`, `1h30m`, `3d`), or a bare integer meaning seconds. `none` is accepted where this spec says so.
- **Identifiers** (node IDs, template names, input names, variable names) match `^[a-z][a-z0-9_]{0,63}$`. Node IDs must not be a reserved word: `end`, `fail`, `self`, `inputs`, `nodes`, `visits`, `vars`, `run`, `env`, `node`, `item`.

### 3.2 Top-level keys

| Key | Type | Required | Description |
|---|---|---|---|
| `arcflow` | integer | no (default `1`) | Format version. Arcflow refuses files with a higher major version than it supports. |
| `name` | identifier-ish string (`^[a-z0-9][a-z0-9_-]*$`) | yes | Flow name, used in run IDs and listings. Need not match the file name. |
| `description` | string | no | One or more sentences; shown in listings and given to authoring agents. |
| `inputs` | map name → input spec | no | Run inputs (§3.3). |
| `defaults` | map | no | Defaults for all nodes, and per node type (§3.4). |
| `limits` | map | no | Run-wide limits (§3.5). |
| `templates` | map name → partial node | no | Reusable node fragments (§3.6). |
| `include` | list of paths | no | Template fragments shared between flows (§3.7). |
| `start` | node ID | no | Entry node. Default: the first key under `nodes`. |
| `nodes` | map ID → node | yes | The graph. Key order is preserved and meaningful only for the default `start` and for display. |
| `outputs` | map name → template string | no | Values computed when the run reaches `end`, stored as the run's outputs and returned by `arcflow run --json` and to a parent `subflow` node. |
| `on_wait` | string (shell command) | no | Hook run when any human node in this flow starts waiting (§5.4). Overrides config. |
| `x-*` | any | no | Ignored extension keys. |

### 3.3 Inputs

```yaml
inputs:
  feature:
    type: string          # string | integer | number | boolean | object | array
    required: true        # default false
    description: What to build.
  max_attempts:
    type: integer
    default: 3
  mode:
    type: string
    enum: [fast, careful]
    default: careful
  config:
    type: object
    schema: schemas/config.json   # optional JSON Schema for object/array inputs (path or inline map)
```

- A required input with no value fails the run's creation with exit code 3, before any node runs.
- Values from the command line are coerced to the declared type (`--input max_attempts=5` becomes the integer 5; `object`/`array` values are parsed as JSON or YAML). A value that doesn't coerce is an error.
- Undeclared inputs are rejected.

### 3.4 Defaults

`defaults` supplies values for node keys. Common keys apply to every node type that accepts them; a key named after a node type holds defaults for that type only.

```yaml
defaults:
  timeout: 20m          # any node type with a timeout
  max_visits: 5
  agent:
    harness: claude
    permissions: edit
    timeout: 45m
  shell:
    timeout: 10m
```

**Precedence**, lowest to highest: built-in default → `defaults.<common key>` → `defaults.<type>.<key>` → template (`extends`, outermost first) → the node's own keys. Merging is described in §3.6.

Built-in defaults that matter for boundedness (brief principle 6):

| Setting | Built-in default |
|---|---|
| `max_visits` (per node) | 10 |
| `limits.max_steps` (visits per run) | 200 |
| `limits.max_duration` (active wall-clock time per run, excluding `waiting`) | 8h |
| `agent.timeout` | 30m |
| `shell.timeout` | 10m |
| `human.timeout` | 7d |
| `sleep` maximum | 7d unless `limits.max_duration` is larger |
| `retry.max_attempts` | 1 (no retry) |
| `agent.schema_retries` | 2 |
| `limits.budget` | `usd: 25` and `tokens: 10_000_000` (whichever is reached first) |

### 3.5 Limits

```yaml
limits:
  max_steps: 100
  max_duration: 4h
  budget:
    usd: 10             # estimated spend across all agent visits in the run
    tokens: 3_000_000   # input + output tokens across all agent visits
```

A limit may be set to `none` to disable it; `arcflow validate` warns when it is (`W-UNBOUNDED`). Budget enforcement is defined in §6.8.

### 3.6 Templates and `extends`

```yaml
templates:
  reviewer:
    type: agent
    harness: codex
    permissions: read-only
    output_schema: schemas/review.json
  strict_reviewer:
    extends: reviewer
    timeout: 20m

nodes:
  review_backend:
    extends: strict_reviewer
    prompt: Review the backend changes on this branch.
```

- A template is a partial node; it may contain any node key except `next` and `on_error` (routing always lives on the node, so the graph is readable from `nodes` alone).
- `extends` names one template; templates may extend templates. Cycles are errors.
- **Merge rule:** mappings merge key by key, recursively; scalars and lists in the more specific definition **replace** the inherited value. `null` removes an inherited key.
- The effective node (after defaults and templates) is what the validator checks and what is recorded in the run's `visit_started` event.

### 3.7 `include`

To keep "one flow = one file" true for the graph, `include` may bring in **templates only**:

```yaml
include:
  - ../shared/reviewers.yaml     # a file whose only top-level key is `templates`
```

Included template names must not collide with local ones. The TUI shows included templates read-only and offers "open fragment" to edit that file. Nodes, inputs and routing can never be included (ADR 0002 amendment, §15 Q3).

### 3.8 Nodes: common keys

Every node accepts these keys; §5 lists type-specific keys.

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | string | required (unless inherited) | Node type (§5). |
| `description` | string | — | Shown in the TUI and given to authoring agents. |
| `extends` | template name | — | §3.6. |
| `next` | target or list of cases | `end` | Where to go after success (§3.9). |
| `on_error` | `fail` \| `continue` \| node ID \| list of cases | `fail` | Where to go after an error outcome (§6.5). |
| `max_visits` | integer | 10 | Visits allowed in one run (§6.4). |
| `timeout` | duration | per type | Wall-clock limit for one attempt. |
| `retry` | map | `{max_attempts: 1}` | §6.6. |
| `on_resume` | `resume` \| `restart` \| `ask` | per type | What resume does with an interrupted visit (§7.5). |
| `workspace` | `shared` \| `worktree` \| map | `shared` | Agent, shell, python nodes only (§6.9). |
| `env` | map name → template string | — | Extra environment for agent, shell, python nodes (§12.3). |
| `x-*` | any | — | Ignored. |

### 3.9 Routing: `next`

```yaml
next: review                      # unconditional

next:                             # conditional: first matching case wins
  - when: nodes.test.exit_code == 0
    to: approve
  - when: visits.implement < 3
    to: implement
  - to: escalate                  # a case without `when` is the default
```

- A target is a node ID or one of the reserved targets **`end`** (the run succeeds) and **`fail`** (the run fails).
- A case may add `reason:` (template string), recorded in the `route_taken` event; for `to: fail` it becomes the run's failure message.
- Cases are evaluated in order; the first whose `when` is true is taken. A default case must be last and there may be only one.
- If no case matches and there is no default, the run fails with `E-NO-ROUTE` at run time; `arcflow validate` warns (`W-NO-DEFAULT-ROUTE`) when a list has no default.
- `when` is an expression (§4). It must evaluate to a boolean; any other type is a run-time error (no truthiness).
- Omitting `next` is equivalent to `next: end`.
- Targets are static strings, never templates, so the graph is known without running anything.

---

## 4. Expressions and templates

### 4.1 Expression language

Expressions are a **restricted subset of Python expression syntax** (ADR 0002), parsed with Python's `ast` module and evaluated by Arcflow's own whitelist evaluator (no `eval`, no builtins, no imports).

Allowed:

- Literals: strings, integers, floats, `True`/`False`/`None`, and also `true`/`false`/`null` as aliases (agents and YAML users write these).
- Lists, dicts and tuples of allowed expressions.
- Names from the state namespace (§4.3); attribute access (`nodes.plan.output.steps`) and subscripts (`nodes["plan"]`, `xs[0]`, `xs[-1]`, slices).
- Operators: `+ - * / // %`, comparisons including chained ones, `in`, `not in`, `is None`, `is not None`, `and`, `or`, `not`, and the conditional expression `a if cond else b`.
- Calls **only** to the functions in §4.2, with positional or keyword arguments.

Rejected at parse time (`E-EXPR-FORBIDDEN`): any name beginning with `_`, attribute names beginning with `_`, method calls (`s.lower()`), lambdas, comprehensions, generator expressions, walrus, starred arguments, f-strings, and expressions longer than 4,000 characters.

**Types** are JSON types mapped to Python: object→dict, array→list, string, number (int or float), boolean, null→None.

**Attribute access on a mapping** reads the key. Reading a **missing key or any attribute of `None` yields `None`** (null-safe navigation), so `nodes.test.stderr` is `None` before `test` has run. Typos are caught statically by the validator (§4.5) rather than at run time.

**Operand rules.** `and`, `or`, `not` and `a if c else b` use Python truthiness; only a routing `when:` must produce a boolean (§3.9). Arithmetic applies to numbers; `+` also joins two strings or two lists, and `*` repeats a string or list by an integer (results are capped at 1,000,000 items). `<`, `<=`, `>`, `>=` compare numbers, or two strings, or two lists. `in` looks into a string, list or object (object keys); any other container is a run-time error, including `None`. Tuples evaluate to lists, and object literal keys must be strings. Reading a list or string index that is out of range yields `None`, like a missing key.

**Errors at run time** (wrong operand types, division by zero, calling a function with bad arguments) make the node that evaluated the expression fail with outcome `failed` and error kind `expression_error`; routing expressions fail the run with `E-EXPR-RUNTIME`, since a routing error means the graph is wrong.

### 4.2 Functions

| Function | Result |
|---|---|
| `len(x)` | length of string, list or object |
| `str(x)`, `int(x)`, `float(x)`, `bool(x)` | conversions (`bool` of a non-boolean is explicit truthiness; `str` renders like interpolation (§4.4), so `str(True)` is `true` and `str(None)` is empty) |
| `abs`, `min`, `max`, `round`, `sum`, `sorted`, `any`, `all` | as in Python, on lists |
| `default(x, fallback)` | `fallback` if `x` is `None` (or an empty string, when `empty=True`) |
| `json(x, indent=2)` | JSON text |
| `from_json(s)` | parsed JSON value |
| `lower(s)`, `upper(s)`, `strip(s)` | string helpers |
| `startswith(s, p)`, `endswith(s, p)` | booleans |
| `matches(s, regex)` | `True` if `re.search` finds a match (regex length ≤ 500) |
| `replace(s, old, new)`, `split(s, sep)`, `join(xs, sep)` | string helpers; `split` without `sep` splits on whitespace, `join` renders non-string items like interpolation |
| `truncate(s, n)` | at most `n` characters, with `…` when cut |
| `head(s, n)`, `tail(s, n)` | first or last `n` lines |
| `keys(o)`, `values(o)` | lists |
| `pluck(xs, key)` | the value of `key` in each object of list `xs` (e.g. `"fail" in pluck(nodes.checks.output, "bucket")`); stands in for comprehensions, which are forbidden |
| `shq(s)` | POSIX shell-quoted string (§12.5) |
| `now()` | current UTC time, ISO 8601 to the second (`2026-09-30T14:15:03Z`) |
| `duration(s)` | seconds from a duration string |

New functions are added only through this list (and the plugin registry for custom nodes is not allowed to add functions in v1), so that flows stay portable.

### 4.3 State namespace

Per ADR 0003:

| Name | Value |
|---|---|
| `inputs.<name>` | Run inputs after defaults and coercion. |
| `nodes.<id>` | The **latest finished visit** of node `<id>`, or `None` if it has not finished a visit. Fields are listed per node type in §5; every node has `outcome`, `visit`, `attempts`, `started_at`, `finished_at`, `duration_s`, `error` (`{kind, message}` or `None`). |
| `nodes.<id>.visits` | List of all finished visits of `<id>`, oldest first, each with the same fields. |
| `visits.<id>` | Number of visits **started** for `<id>`, including the current one. `0` if never visited. |
| `vars.<name>` | Variables set by `set` nodes. `None` when unset. |
| `run` | `id`, `flow` (name), `flow_file`, `started_at`, `workdir`, `budget` (`{usd_spent, tokens_spent, usd_left, tokens_left}`; `None` for unknown or unlimited values). |
| `env.<NAME>` | Environment variables of the runner process that are allowed by §12.3. Reading any other variable yields `None`. |
| `node` | Inside a node's own configuration only: `id`, `visit`, `attempt`, `artifacts_dir`, `workdir`, and for human nodes `message`. |
| `item`, `index` | Inside `map` bodies only. |
| `self` | Alias for `nodes.<current node id>`, usable in the node's own `next` and `on_error` (anywhere else it is `E-UNKNOWN-REF`). |

`visits.<id>` counts started visits so that `when: visits.implement < 3` read in a later node means "implement has run fewer than three times".

The normalized result field is named **`outcome`**, not `status`, so it does not clash with the `status` field agents are encouraged to put in their own structured output (`nodes.x.output.status`) (ADR 0003 amendment, §15 Q1).

### 4.4 Templates (`${{ }}`)

Strings in templated fields (marked "T" in §5) may contain `${{ expression }}`.

- **Whole-value rule.** If a field's entire value is a single `${{ … }}` with nothing around it, the result keeps its type (a list stays a list). This matters for non-string fields such as `subflow.inputs`, `set.vars`, `sleep.until`, `map.items`.
- **Interpolation rule.** Otherwise each `${{ }}` is replaced by its value rendered as text: strings as-is, `None` as the empty string, booleans as `true`/`false`, numbers as JSON, lists and objects as indented JSON (`json(x)`).
- `$${{` produces a literal `${{`.
- Templates are rendered **once, at the start of each visit** (and again for each retry attempt), and the rendered values are recorded (§7.2), so resume never re-renders a finished visit.
- Brief-style `{{ x | filter }}` syntax is not supported; `arcflow validate` flags `{{` without `$` as `W-JINJA-LIKE` since it is almost always a mistake.
- `prompt_file`, `instructions_file` and `message_file` contents are templates too.

### 4.5 Static checks on expressions

`arcflow validate` parses every expression and template and checks each reference:

- `inputs.x`: `x` is a declared input.
- `nodes.x`: `x` is a node ID. `nodes.x.<field>`: the field exists for that node's type (§5). `nodes.x.output.<path>`: when `x` has an `output_schema`, the path exists in it (following `properties`, `items`, and `$ref` within the same file); when it has none, access to `output` is allowed but unchecked (`I-UNCHECKED-OUTPUT`).
- `visits.x`: `x` is a node ID.
- A reference to a node that cannot have run before the current node on any path (no path from it to here) is a warning (`W-NEVER-SET`), since it will always be `None`. For flow `outputs`, "here" is `end`.
- `self` outside `next`/`on_error`, `node` in flow `outputs`, and `item`/`index` outside a `map` node's `inputs` are `E-UNKNOWN-REF`.
- `E-NO-EXIT` follows every `next` edge (including the implicit `next: end`) and the `on_error` edges the author wrote; the implicit `on_error: fail` does not count as a way out, or no node could ever lack an exit.

Types are not inferred beyond this in v1.

---

## 5. Node catalog

Key tables use: **T** = templated field (§4.4). Every node also has the common keys of §3.8 and the common result fields of §4.3.

| Type | Tier | Purpose |
|---|---|---|
| `agent` | Core | Run a harness session. |
| `shell` | Core | Run a command. |
| `condition` | Core | Branch without doing work. |
| `human` | Core | Pause for a choice, text or acknowledgement. |
| `sleep` | Core | Wait for a duration or until a time. |
| `set` | Core | Assign variables. |
| `python` | Core | Call a Python function. |
| `subflow` | Core | Run another flow file as a child run. |
| `map` | Core | Run a subflow once per list item (sequential in v1, ADR 0007). |
| `handoff` | Core | Hand an agent session to the user interactively, continue when they exit. |
| `notify` | Core | Send a notification through a command or webhook. |
| `parallel`, `join`, `git`, `http`, `wait_for` | Future | Reserved. |

### 5.1 `agent` (Core)

```yaml
plan:
  type: agent
  harness: claude
  model: sonnet
  permissions: read-only
  prompt_file: prompts/plan.md
  output_schema: schemas/plan.json
  timeout: 20m
  budget: { usd: 3 }
  next: implement
```

| Key | Type | Default | Description |
|---|---|---|---|
| `harness` | string | `defaults.agent.harness`, else required | Adapter name: `claude`, `codex`, `fake`, or a custom adapter (§8). |
| `model` | string T | harness default | Passed through; Arcflow does not interpret model names. |
| `effort` | `low` \| `medium` \| `high` \| `max` | harness default | Mapped by the adapter when supported, else `W-IGNORED-OPTION`. |
| `prompt` / `prompt_file` | string T / path | exactly one required | The task. Sent on stdin (Claude, Codex) or as the adapter requires. |
| `instructions` / `instructions_file` | string T / path | — | Extra system instructions (Claude `--append-system-prompt-file`; Codex: prepended to the prompt under a heading, since `codex exec` has no equivalent flag). |
| `output_schema` | path or inline map | — | JSON Schema (draft-07) the final output must match (§5.1.2). |
| `schema_retries` | integer | 2 | Extra turns allowed to fix invalid output. |
| `session` | `new` \| `continue` \| `{resume: <id>}` \| `{fork: <id>}` | `new` | §5.1.3. |
| `permissions` | `read-only` \| `edit` \| `full` | `edit` | Profile (§8.4, ADR 0005). |
| `allow_tools` / `deny_tools` | list of strings | — | Harness tool rules, passed through (Claude `--allowedTools`/`--disallowedTools`; Codex: `W-IGNORED-OPTION`). |
| `add_dirs` | list of paths T | — | Extra directories the harness may access. |
| `max_turns` | integer | — | Passed to harnesses that support it. |
| `budget` | `{usd, tokens}` | — | Per-visit cap, in addition to the run budget (§6.8). |
| `bare` | boolean | `false` | Claude only: `--bare` (ignore the host's CLAUDE.md, hooks, plugins). Other adapters ignore it with a warning. |
| `harness_options` | map | — | Adapter-specific pass-through, validated by the adapter's own schema (§8.2). |
| `on_resume` | | `resume` | §7.5. |

**Result fields** (`nodes.<id>.…`):

| Field | Type | Description |
|---|---|---|
| `output` | any | Validated structured output, or `None` without `output_schema`. |
| `text` | string | The final assistant message. |
| `session_id` | string | Harness session ID. |
| `harness`, `harness_version`, `model` | string | As reported by the harness. |
| `usage` | object | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens` (missing counters are 0). |
| `cost_usd` | number or `None` | This visit's spend. |
| `cost_estimated` | boolean | `true` when computed from Arcflow's price table or reported by the harness as a client-side estimate (always true in v1: see research §1.2). |
| `num_turns` | integer or `None` | |
| `permission_denials` | list | `{tool, reason}` entries reported by the harness (ADR 0005). |
| `schema_errors` | list | Remaining validation errors when outcome is `schema_invalid`. |
| `workspace` | object | `{path, branch}` (§6.9). |
| `artifacts_dir` | string | §6.10. |

#### 5.1.1 Success

An agent attempt **succeeds** only when all of these hold: the adapter received a final result event, the harness reported success in it, and (with `output_schema`) the output validates. Exit codes alone never decide (ADR 0011). Anything else produces the matching outcome: `failed`, `timed_out`, `cancelled`, `budget_exceeded`, `schema_invalid`.

Flows that care whether the agent really did the work should require a `status` field in the output schema and branch on it (research §1.5.7); Appendix A shows the pattern.

#### 5.1.2 Structured output

- Schemas use **JSON Schema draft-07**, the newest dialect `fastjsonschema` implements (ADR 0001 rules out the native-code validators that support later drafts). A `$schema` naming another dialect is `E-BAD-JSON-SCHEMA`, and so is a remote `$ref`: schemas must be self-contained. Schema files are JSON.
- The schema file is loaded and checked as a valid JSON Schema at validate time.
- If the adapter declares native structured output, the schema is passed to the harness (Claude `--json-schema`, Codex `--output-schema`). Arcflow validates the result again with `fastjsonschema` in all cases.
- Without native support, Arcflow appends an instruction to reply with only a JSON object matching the schema, extracts the last fenced or bare JSON object from the final text, and validates it.
- On failure, if `schema_retries` remain: the adapter resumes the same session (when it can) with a message listing the validation errors and asking for corrected JSON only; otherwise it re-runs the prompt with the errors appended. Each fix is recorded as `schema_retry`, counts toward the visit's budget but not toward `retry.max_attempts`, and happens within the same attempt, whose `usage` and `cost_usd` are the sums over all its calls.
- Without an `output_schema`, `output` is `None` whatever the harness returned. Extraction takes the last fenced JSON block, else the last top-level JSON object in the final text.
- For `harness: codex`, `validate` warns (`W-CODEX-STRICT-SCHEMA`) when the schema lacks `additionalProperties: false` on objects or does not list every property in `required`, pending the M3 contract test (research §7.3).

#### 5.1.3 Sessions

| `session` | Behaviour |
|---|---|
| `new` | A fresh session. Adapters that let the caller choose IDs (Claude) receive a new UUIDv4 from Arcflow. |
| `continue` | On the node's second and later visits, resume the session of its own previous visit, so a fix loop keeps its context. First visit behaves as `new`. |
| `{resume: <id>}` | Resume the latest session of node `<id>`, which must use the same harness (`E-SESSION-HARNESS`). The harness's view of the session changes. |
| `{fork: <id>}` | Start a copy of node `<id>`'s latest session (Claude `--resume … --fork-session --session-id <new>`; Codex `exec fork`). Falls back to `resume` with a warning when the adapter lacks `fork`. |

When the referenced node has not run, `resume`/`fork` behave as `new` and log a warning event (`W-SESSION-NEW`). When the adapter cannot resume, `continue`/`resume` behave as `new` and `validate` warns (`W-IGNORED-OPTION`); a `fork` the adapter cannot do resumes instead, with a `W-FORK-AS-RESUME` warning event. The session chosen for each attempt is recorded in `attempt_started` (`adapter`, `session_mode`, `resume_session_id`).

**Cost on resume.** Adapters report the visit's own spend (the delta), not a session total (research §1.2, "Resume cost semantics").

### 5.2 `shell` (Core)

```yaml
test:
  type: shell
  run: npm test -- --reporter=dot
  timeout: 15m
  on_error: continue
```

| Key | Type | Default | Description |
|---|---|---|---|
| `run` | string T | one of `run`/`args` | Script run by `shell` (below). |
| `args` | list of strings T | — | Program and arguments, executed **without** a shell. Preferred when interpolating anything that came from an agent (§12.5). |
| `shell` | string | `bash` if found, else `sh` | Interpreter for `run`, invoked as `<shell> -eo pipefail -c` (`-e -c` for `sh`). |
| `cwd` | path T | the node's workspace | Relative paths resolve against the workspace. |
| `stdin` | string T | empty | Text fed to standard input. |
| `ok_codes` | list of integers | `[0]` | Exit codes that count as success. |
| `output` | `none` \| `json` \| `text` | `none` | `json`: parse stdout as JSON into `output` (a parse failure is outcome `failed`, kind `output_parse`). `text`: `output` is stdout. |
| `output_schema` | path or inline map | — | With `output: json`, validate it (outcome `schema_invalid` on failure). |
| `max_output` | size (`64KiB`) | `64KiB` | How much of stdout/stderr (the **tail**) is kept in state; full streams are always in files. |
| `on_resume` | | `restart` | §7.5. |

Result fields: `exit_code` (integer, or `None` if killed), `signal` (name or `None`), `stdout`, `stderr` (tails), `stdout_file`, `stderr_file` (absolute paths), `stdout_truncated`, `stderr_truncated`, `output`, `workspace`, `artifacts_dir`.

Behaviour: `bash`, `zsh` and `ksh` given as `shell` run with `-eo pipefail`, any other interpreter with `-e`. Output is written to `stdout.log`/`stderr.log` as it arrives, as UTF-8 text with `redact` patterns applied line by line (§12.4). The command runs in its own process group with the node environment (§12.3) plus `ARCFLOW_RUN_ID`, `ARCFLOW_RUN_DIR`, `ARCFLOW_NODE_ID`, `ARCFLOW_VISIT`, `ARCFLOW_ATTEMPT`, `ARCFLOW_ARTIFACTS_DIR`. An exit code outside `ok_codes` is outcome `failed` with error kind `exit_code`. Timeouts and cancellation stop the whole process group (§6.7).

### 5.3 `condition` (Core)

A node that does nothing but route. Its `next` must be a list with a default case.

```yaml
check_coverage:
  type: condition
  next:
    - when: nodes.test.output.coverage >= 0.8
      to: approve
    - to: add_tests
```

Result fields: `branch` (target taken). A condition node's visit always succeeds unless an expression errors.

### 5.4 `human` (Core)

```yaml
approve:
  type: human
  message: |
    Tests pass. Merge ${{ inputs.branch }} into main?
  choices: [merge, reject]
  input: text            # also allow a free-text comment
  timeout: 3d
  default: reject        # taken on timeout
  next:
    - when: nodes.approve.choice == "merge"
      to: merge
    - to: end
```

| Key | Type | Default | Description |
|---|---|---|---|
| `message` / `message_file` | string T / path | required | What the person sees. Markdown is rendered in the TUI and shown as plain text in the CLI. |
| `choices` | list of strings, or of `{value, label}` | — | Options; the answer must be one `value`. |
| `input` | `none` \| `text` | `none` if `choices`, else `text` | Whether free text is accepted (required when there are no choices). |
| `ack` | boolean | `false` | With neither choices nor text: the person only acknowledges. Mutually exclusive with `choices`/`input: text`. |
| `timeout` | duration or `none` | 7d | How long to wait. |
| `default` | a choice value | — | Answer recorded on timeout. Without it, a timeout is outcome `timed_out`. |
| `show` | list of template strings | — | Extra context lines shown with the prompt (e.g. `${{ nodes.plan.output.summary }}`). |
| `on_wait` | string T (shell command) | flow `on_wait`, else config `on_wait` | Notification hook run once when waiting starts (ADR 0006). |

Result fields: `choice`, `text`, `acknowledged`, `responder` (from `--as`, else `$USER`), `responded_at`, `via` (`cli`, `tui`, `timeout`), `timed_out`.

**Answers.** With `choices`, the answer must be one choice value; free text (`--comment`) is accepted alongside only with `input: text`. Without choices, `input: text` needs non-blank text. With `ack`, only an acknowledgement is accepted. A timeout is recorded as `human_responded` with `via: timeout` and the `default` as its choice (none without a default, and the visit's outcome is then `timed_out`). The visit's deadline is recorded in `visit_started`.

**Waiting.** On entering a human node the runner records `human_waiting`, runs the `on_wait` hook (its failure is logged, never fatal; it runs detached with a 30 s timeout and receives `ARCFLOW_RUN_ID`, `ARCFLOW_NODE_ID`, `ARCFLOW_MESSAGE`, `ARCFLOW_RESPOND_CMD`), sets the run to `waiting`, and then either keeps the process alive or exits (§6.11). An answer arrives through `arcflow respond` or the TUI (§7.4). Validation of an answer (choice in list, text present) happens at `respond` time; an invalid answer is rejected with exit code 2 and changes nothing.

### 5.5 `sleep` (Core)

```yaml
wait_for_ci:
  type: sleep
  duration: 10m          # or:
  # until: ${{ inputs.start_at }}   # ISO 8601 timestamp; past times return at once
```

Exactly one of `duration` (duration, T) or `until` (timestamp, T). The runner records `wake_at` in the `visit_started` event, so a resumed run sleeps only for the remainder. A sleep longer than the remaining `limits.max_duration` fails validation when static and fails the visit at run time otherwise. Result fields: `woke_at`. While sleeping the run stays `running`; `arcflow run --detach` is the way to leave a long sleep unattended.

### 5.6 `set` (Core)

```yaml
bump:
  type: set
  vars:
    attempt: ${{ default(vars.attempt, 0) + 1 }}
    last_error: ${{ tail(nodes.test.stderr, 20) }}
```

Evaluates every value (whole-value rule applies) against the state **before** the node, then assigns them together. Result fields: `values`.

### 5.7 `python` (Core)

```yaml
coverage_ok:
  type: python
  call: tools.checks:coverage_ok      # module:function, importable from the project root
  args:
    report: ${{ nodes.test.output }}
    threshold: 0.8
  output_schema: schemas/coverage.json
```

- Runs in a **child process**, not in the runner, so timeouts and cancellation work and a crash cannot corrupt the runner. The child runs Arcflow's stdlib-only `pycall.py` script by path, so the node's `interpreter:` key can select any Python 3 executable, with or without Arcflow installed (default: the one running Arcflow); the project root is first on `sys.path`, and the process runs in the node's workspace.
- The function receives `args` as keyword arguments (JSON values) and, when its signature accepts `ctx` (or `**kwargs`), a read-only `ctx` with the attributes `run_id`, `node_id`, `visit`, `artifacts_dir`, `workdir`. Its prints go to the attempt's `stdout.log` and `stderr.log`. It returns a JSON-serializable value, which becomes `output`. An exception is outcome `failed` with kind `exception` and the traceback in the visit directory.
- Arcflow never reads or edits the function body; the TUI shows `call` as a reference that opens the module in `$EDITOR` (research §8.2).

### 5.8 `subflow` (Core)

```yaml
triage_one:
  type: subflow
  flow: triage-issue.yaml             # relative to this flow file
  inputs:
    issue: ${{ nodes.fetch.output[0] }}
```

Starts a **child run** of the referenced flow with its own run directory, linked by `parent` in `run.json` and by `child_run` in the parent's events. The child runs in the parent's runner process. The parent's remaining budget and `max_duration` are passed down as the child's limits (the smaller of the two wins; recorded as `limits_cap` in the child's `run.json`), and the child's spend is added to the parent's totals when it ends. Result fields: `run_id`, `status`, `outputs` (the child flow's `outputs`), and `output` (alias of `outputs`). A failed child is outcome `failed`, error kind `child_failed`, as is a child that cannot be created (invalid flow or inputs). Cancelling the parent cancels the child; a `timeout` on the node cancels the child and the outcome is `timed_out`. A human node in the child makes the parent `waiting` as well: the parent records a `human_waiting` with `kind: "child"`, `child_run` and `child_node`, and `arcflow respond` on the parent's run ID forwards the answer to the child. Recursion depth is limited to 8 (`depth` in `run.json`). The child flow is part of the parent's snapshot, with its own referenced files. `validate` loads the child flow, reports its errors, and checks that the node passes only declared inputs and every required one.

### 5.9 `map` (Core)

```yaml
fix_each:
  type: map
  items: ${{ nodes.lint.output.files }}
  flow: fix-file.yaml                  # the body is a subflow
  inputs:
    path: ${{ item }}
  max_items: 50                        # default 100; more items fail the node
  on_item_error: continue              # fail (default) | continue
```

Runs one child run per item, **sequentially** in v1 (ADR 0007), with everything `subflow` does for each. `item` and `index` are available in `inputs`, which are rendered once per item (not at visit start). Result fields: `results` (list of `{index, run_id, status, outputs}`), `succeeded`, `failed` (counts). With `on_item_error: fail` the node fails at the first failed item; with `continue` it runs every item and succeeds. A `concurrency` key is reserved and must be `1` in v1 (`E-SCHEMA` otherwise). `items` must evaluate to a list.

### 5.10 `handoff` (Core)

```yaml
take_over:
  type: handoff
  from: implement            # an agent node whose latest session is handed over
  message: Tests keep failing. Over to you; exit the session to continue the flow.
```

When the runner is in the foreground with a person at the terminal (`--on-wait prompt`, the default on a TTY), Arcflow prints the message and runs the adapter's interactive command (Claude `claude --resume <id>`, Codex `codex resume <id>`) attached to the terminal, in the `from` node's workspace, then continues when it exits. Otherwise the run becomes `waiting` like a human node (a `human_waiting` of `kind: "handoff"` carrying `session_id` and `command`), and `arcflow handoff <run> [<node>]` opens the session later, records its exit code (`human_responded` with `via: "handoff"`), and continues the run in the foreground (`--no-continue` to only open it). Result fields: `session_id`, `exit_code`. Requires adapter capability `interactive`; `validate` rejects a `from` that is not an agent node (`E-SCHEMA`), does not exist (`E-UNKNOWN-REF`), or uses an adapter without it (`E-SCHEMA`). A `from` node that has no session yet fails the visit.

### 5.11 `notify` (Core)

```yaml
tell_me:
  type: notify
  message: "Review finished: ${{ nodes.review.output.verdict }}"
  command: 'notify-send "Arcflow" "$ARCFLOW_MESSAGE"'        # or:
  # webhook: { url: "${{ env.SLACK_WEBHOOK }}", body: { text: "${{ node.message }}" } }
```

Keys: `message` (T, required; passed to `command` as `ARCFLOW_MESSAGE` and available as `node.message` in the node's other fields), exactly one of `command` (run like `on_wait`, 30 s timeout) or `webhook` (`{url, method: POST, headers, body}`, JSON body, 10 s timeout; any 2xx status is success), and `required` (default `false`). Fire-and-forget: a failure is recorded as a `W-NOTIFY-FAILED` warning event and the node still succeeds unless `required: true`. The node has no type-specific result fields.

---

## 6. Execution semantics

### 6.1 Run lifecycle

1. **Create.** Resolve the flow file, parse and validate it (errors abort with exit code 3), coerce inputs, allocate a run ID (§7.1), create the run directory, copy the flow file and every file it references (`prompt_file`, schemas, templates from `include`) into `snapshot/`, write `run.json`, append `run_created`.
2. **Start.** Acquire the run lock (§7.4), append `run_started`, set the current node to `start`.
3. **Step loop** (§6.2) until a terminal target, a wait, a cancel, or a limit.
4. **Finish.** Evaluate `outputs` on `end` (an error there fails the run), append `run_succeeded`/`run_failed`/`run_cancelled`, release the lock.

A run always executes the **snapshot**, never the live flow file, so editing a flow in the TUI or by an agent never changes a run in progress. `arcflow resume --reload` is the explicit way to continue with an edited flow (§7.6).

### 6.2 The step loop

For the current node `N`:

1. **Limits.** If `visits.N` is already `max_visits`, the run fails with `max_visits_exceeded`. If the run has made `limits.max_steps` visits, it fails with `max_steps_exceeded`. If active time exceeds `limits.max_duration`, it fails with `max_duration_exceeded`. If `N` is an agent node and the run budget is exhausted, it fails with `budget_exceeded` (§6.8).
2. **Start the visit.** Increment `visits.N`; render templated fields; append `visit_started` with the effective, rendered configuration (large values such as prompts are written to the visit directory and referenced by path and SHA-256). If rendering fails, the visit is still recorded: it finishes at once with outcome `failed`, error kind `expression_error` and zero attempts, and is routed by `on_error` like any failure.
3. **Attempts.** Run attempt 1; on an error outcome, apply `retry` (§6.6) until an attempt succeeds or attempts run out.
4. **Finish the visit.** Append `visit_finished` with the visit's result fields. The node's entry in `nodes.N` is replaced; the previous one moves into `nodes.N.visits`.
5. **Route.** If the outcome is `succeeded`, evaluate `next`; otherwise evaluate `on_error` (§6.5). Append `route_taken {from, to, case_index, reason}`.
6. **Continue** with the target, or finish on `end`/`fail`.

Every append in steps 2, 4 and 5 is flushed and `fsync`ed before the runner continues, and `state.json` is rewritten atomically after step 5. Progress events inside a visit (agent streaming, §7.2) are flushed but not `fsync`ed.

### 6.3 Data passing

Nodes communicate only through state (§4.3) and the filesystem (the workspace and artifact directories). There are no implicit inputs: an agent sees another node's output only if its prompt references it. Rendered templates are recorded, so the exact text each agent received is always inspectable (`arcflow logs --node plan --prompt`).

**Size limits in state.** String result fields are capped: `stdout`/`stderr` by `max_output` (tail kept), agent `text` at 256 KiB (head kept, full text in the visit directory), and `output` objects at 1 MiB serialized (larger output is outcome `failed`, kind `output_too_large`). This keeps `state.json` and prompts bounded.

### 6.4 Loops and visit limits

Cycles are ordinary edges back to an earlier node. Each node allows `max_visits` visits per run (default 10); the run allows `limits.max_steps` visits in total (default 200). Exceeding either fails the run, naming the node, so an unguarded loop always terminates. Flows should guard loops with `visits.<id>` in a `when:` so they can route somewhere useful (an escalation node) before the hard limit; `arcflow validate` warns (`W-UNGUARDED-CYCLE`) for a cycle in which no `when:` of a node in the cycle (including cases that leave it) references `visits` of a node in the cycle.

### 6.5 Errors and `on_error`

A visit whose final attempt is not `succeeded` is an **error**. `on_error` decides what happens:

| `on_error` | Effect |
|---|---|
| `fail` (default) | The run fails with the node's error. |
| `continue` | Route with `next` as if the node succeeded; the outcome is still recorded, so `next` can branch on `nodes.<id>.outcome` or `exit_code`. |
| `<node id>` | Go to that node. |
| list of cases | Like `next`, evaluated with the failed visit in state. A list without a default falls back to `fail`. |

`cancelled` is never routed: cancellation stops the run. `interrupted` exists only for visits cut off by a runner crash and is handled by resume (§7.5), not by `on_error`.

`arcflow validate` warns (`W-ERROR-NEVER-ROUTED`) when a node's `next` references its own `exit_code` or `outcome` but `on_error` is `fail`, since the failing branch could never be taken. This is the typical mistake with a test step in a fix loop; Appendix A shows the right pattern (`on_error: continue`).

Error kinds (`nodes.<id>.error.kind`): `exit_code`, `harness_error`, `no_result`, `timeout`, `budget`, `schema`, `output_parse`, `output_too_large`, `expression_error`, `exception`, `spawn_failed`, `workspace_error`, `limit`, `child_failed`.

### 6.6 Retries

```yaml
retry:
  max_attempts: 3                     # total attempts, including the first
  backoff: 30s                        # delay before attempt 2; doubles each time, max 10m
  on: [failed, timed_out]             # outcomes that trigger a retry (default: [failed, timed_out])
```

- Retries repeat the **same visit** with freshly rendered templates (state is unchanged, so they render the same unless they use `now()`).
- Agent retries start a new session unless `session` is `continue`/`resume`, in which case they resume the session the failed attempt used when one was recorded.
- `budget_exceeded`, `cancelled` and `schema_invalid` are never retried by `retry` (schema problems have `schema_retries`).
- Each attempt has its own subdirectory and its own `attempt_started`/`attempt_finished` events.

### 6.7 Timeouts and cancellation

- `timeout` limits one attempt's wall-clock time. The runner enforces it; harness timeouts are never relied on (ADR 0011).
- **Stopping a process** (timeout, cancel, runner shutdown): send `SIGINT` to the process group; wait up to 10 s (configurable per adapter as `grace`) for the adapter to report a final result; then `SIGTERM`; after 5 more seconds `SIGKILL`. The visit records which step ended it (`stopped_by`). A result that arrives during the grace period is kept (partial usage and cost still count toward the budget).
- **Cancel** (`arcflow cancel`, TUI) stops the current attempt as above, records outcome `cancelled`, skips routing and ends the run `cancelled`. A waiting run, or any run without a live runner, is cancelled immediately by the cancelling process. With a live runner the request goes through the inbox, and `arcflow cancel` waits up to 30 s for the run to end cancelled. It prints `{run_id, status, delivered: "requested" | "cancelled"}` with `--json` and exits 0; a run that already finished is exit 2 (`E-ALREADY-FINISHED`).
- **Runner signals.** `SIGINT`/`SIGTERM` to a foreground `arcflow run` stops the current attempt and leaves the run **resumable** (`runner_detached` event, status stays `running`, displayed as `interrupted`), rather than cancelling it. A second `SIGINT` within 3 s skips the grace period. Use `arcflow cancel` to end a run for good.
- On Windows (unsupported in v1, §15 Q10) process groups are replaced by job objects.

### 6.8 Budgets and cost

Budgets exist at two levels: the run (`limits.budget`) and the agent visit (`budget` on the node). Each accepts `usd`, `tokens`, or both (ADR 0011).

- **Accounting.** After every call to the harness (each attempt, and each schema fix within it), its `usage` and `cost_usd` are added to the run's totals (`budget_updated` event). USD comes from the harness where it reports it (Claude `total_cost_usd`, delta on resume), otherwise from `prices` in config (§2.2), otherwise it is unknown. Prices apply per million tokens, with `cached_input_tokens` counted as part of `input_tokens` (adapters normalize to this) and billed at `cached_input_per_mtok` when given; when the model has no entry and the harness has exactly one, that one is used. `tokens` budgets count `input_tokens + output_tokens`. A node's budget covers its whole visit, across attempts, schema fixes and a resume.
- **Before a visit starts:** if either run total has reached its limit, the run fails with `budget_exceeded` (not routed; the run budget is a hard stop).
- **During a visit:** the adapter receives the remaining allowance, `min(node budget, run budget − spent)`, and passes it to the harness when it has a budget cap (Claude `--max-budget-usd`) as a backstop. Arcflow also watches streamed usage and stops the attempt (§6.7) when the node or run budget is crossed. Overshoot of one model response is expected (research §1.4) and reported.
- **Unknown USD.** When a node has a `usd` budget but its adapter reports no cost and no price is configured, `validate` warns (`W-USD-UNENFORCEABLE`) and only the `tokens` budget is enforced for that node.
- **Estimates.** All USD figures are labelled estimates in the CLI and TUI (`~$1.24`). Subscription users can budget in tokens only by setting `usd: none`.

### 6.9 Workspaces

The **run workdir** is the directory given by `arcflow run --workdir`, default the project root. Nodes with `workspace: shared` (the default) run there, and see each other's changes (ADR 0004).

`workspace: worktree` gives a node a **Arcflow-managed git worktree** (ADR 0004):

```yaml
workspace: worktree                 # a worktree private to this node, reused by all its visits
workspace:
  worktree: feature                 # a named worktree, shared by every node that names it in this run
  base: ${{ inputs.base_branch }}   # commit-ish to branch from (default: HEAD of the run workdir at run start)
  branch: arcflow/${{ run.id }}/feature  # default: arcflow/<run-id>/<name>
  keep: true                        # default true; false removes it when the run succeeds
```

- Created on first use with `git worktree add -b <branch> .arcflow/worktrees/<run-id>/<name> <base>` (under the project root). `<name>` defaults to the node ID. The default base is the run workdir's `HEAD` when the run was created, recorded as `git_head` in `run.json`. Base commit and branch are recorded (`workspace_created`) so resume reuses the same worktree. When several nodes name one worktree, the first to run creates it and its `base`/`branch` apply.
- `keep: false` worktrees are removed with `git worktree remove --force` when the run succeeds (`workspace_removed`); their branches stay. `--recreate-workspaces` recreates a missing worktree from its recorded branch, so only what was committed there comes back.
- A worktree that cannot be created fails the visit with error kind `workspace_error`.
- The run workdir must be inside a git repository, otherwise `E-WORKSPACE-NO-GIT` at validate/run start.
- Arcflow never merges, pushes or deletes branches by itself; flows do that with shell nodes (Appendix A, example 2). `arcflow gc` removes worktrees of finished runs older than `retention.keep_days` whose `keep` is false or whose branch is fully merged.
- Harness-native worktree flags (`claude -w`, `codex --worktree`) are not used.
- Result fields `workspace.path` and `workspace.branch` let later nodes (including `shared` ones) refer to a worktree.

### 6.10 Artifacts

Each visit has `nodes/<id>/<visit>/artifacts/` in the run directory, exposed as `node.artifacts_dir` in templates, `ARCFLOW_ARTIFACTS_DIR` in the environment, and `nodes.<id>.artifacts_dir` to later nodes. Agents are told the path only if the prompt mentions it. `arcflow artifacts <run-id> [--node N]` lists them; the TUI shows them in the node inspector. Artifacts are never deleted before the run directory itself.

### 6.11 Waiting for humans and the runner process

When a run reaches a human node (or a `handoff` without a TTY), what the runner process does depends on `--on-wait`:

| `--on-wait` | Behaviour | Default when |
|---|---|---|
| `prompt` | Ask on the terminal inline (the same answer `arcflow respond` would record), while also accepting answers from other clients. | foreground run with a TTY on stdin |
| `wait` | Keep the process alive, polling the run's inbox (§7.4) once per second, until answered. | — |
| `exit` | Release the lock and exit with code **4** (`waiting`). | `--detach`, or no TTY |

Whoever answers later (`arcflow respond`, the TUI) records the answer, and if no runner holds the lock, **starts a detached runner** to continue the run (`--no-continue` to only record). A runner that exits at a wait records `runner_detached` with `reason: "waiting"`. A detached runner is `arcflow resume <run> --on-wait exit` in its own session, writing its output to `runner.log` in the run directory; `arcflow run --detach` and `arcflow resume --detach` start one and return at once. A resumed human or sleep visit continues its open attempt rather than starting a new one.

`arcflow respond` prints `{run_id, node, delivered: "inbox" | "recorded", continued}` with `--json`; it exits 2 (`E-INVALID-ANSWER`) for an answer that does not fit, 6 when the run or node is not waiting, 7 when the lock is held but not live enough to take. This is how a CI job or cron entry can start a flow, exit at an approval step, and have the flow finish after a person responds hours later, with no daemon (ADR 0008).

A human node's `timeout` is enforced by whichever runner holds the run; if none does, it is enforced when the next `arcflow status`, `list`, `respond`, `resume` or TUI refresh touches the run (lazy timeout). A scheduled `arcflow resume --due` (cron recipe in the docs) makes timeouts fire without a person touching the run.

---

## 7. Persistence and resume

### 7.1 Run IDs

`<UTC timestamp>-<flow name>-<4 random base32 chars>`, e.g. `20260930T141503-implement-feature-7k2q`. Sortable by start time to the second; runs created in the same second are ordered by `created_at` in `run.json`. CLI arguments accept any unique prefix or suffix, and `@last` / `@last:<flow>` for the most recent run.

### 7.2 Run directory

```
.arcflow/runs/<run-id>/
  run.json              # immutable: id, flow name, flow path, flow SHA-256, arcflow version,
                        #   inputs, workdir, created_at, parent run (subflow), host, user
  snapshot/             # the flow file and every file it references, as run
  events.jsonl          # append-only source of truth (ADR 0012)
  state.json            # derived snapshot for fast reads; rebuildable from events
  lock                  # runner lock (§7.4)
  inbox/                # requests from other processes (§7.4)
  nodes/<id>/<visit>/
    visit.json          # effective rendered config (large fields by reference)
    prompt.md           # agent: rendered prompt; instructions.md when set
    attempt-<k>/
      stream.jsonl      # agent: raw harness stream, byte for byte
      stdout.log  stderr.log
      result.json       # the attempt's normalized result
    artifacts/
```

The directory is created with mode `0700`. `state.json` is written by write-to-temp-then-rename. `events.jsonl` is the only file whose loss makes a run unrecoverable; everything else is either immutable or derivable.

### 7.3 Event log

Each line is a JSON object with at least:

```json
{"v": 1, "seq": 17, "ts": "2026-09-30T14:16:02.113Z", "type": "visit_finished",
 "branch": "main", "node": "test", "visit": 2, "data": { … }}
```

- `seq` increases by one per event; a gap or duplicate is corruption.
- `branch` is always `"main"` in v1; it is the key that parallel branches will use (ADR 0007).
- A final line that is not valid JSON (torn write after a crash) is ignored and reported as a warning; any other invalid line is corruption and blocks resume until repaired (`arcflow doctor <run>`).
- Event types are listed in Appendix B. The set is versioned by `v`; readers must ignore unknown types, so new ones can be added without a version bump.

`state.json` contains `{status, current, nodes, visits, vars, totals, pending_human, last_seq}` and is valid iff its `last_seq` equals the last event's `seq`; otherwise readers rebuild it from events.

### 7.4 Concurrency: locks and the inbox

- **Single writer.** Only the process holding the run lock appends to `events.jsonl`.
- **Lock.** `lock` is created with `O_CREAT|O_EXCL` and holds `{pid, host, started_at, heartbeat_at}`; the holder refreshes `heartbeat_at` every 5 s. A lock is **stale** when its host is this host and the PID is gone, or when `heartbeat_at` is older than 30 s. Taking over a stale lock is recorded (`runner_takeover`).
- **Inbox.** Other processes (`arcflow respond`, `arcflow cancel`, the TUI) never append events while a runner is live. They write a request file into `inbox/` (atomic rename, named `<ts>-<random>.json`); the runner consumes it within one second, appends the resulting event, and deletes the file. If no live runner exists, the requesting process takes the lock itself, applies the request, and (for `respond`) starts a detached runner unless told not to.
- **Readers** (status, logs, TUI) need no lock: they read `state.json` and tail `events.jsonl`.
- Run directories on network filesystems are unsupported (lock semantics); `arcflow doctor` warns.

### 7.5 Resume

`arcflow resume <run>` takes the lock, rebuilds state from events, and continues:

- **Finished visits are never rerun** (ADR 0012). Their recorded results and routing decisions are reused as is.
- **A visit that started but did not finish** is `interrupted`. What happens depends on its node's `on_resume`:

| `on_resume` | Behaviour | Default for |
|---|---|---|
| `resume` | Agent: if a session ID was recorded and the adapter can resume, start a new attempt that resumes that session with the prompt "Your previous run was interrupted. Continue the task and finish with the required output." Otherwise behaves as `restart`. Other types: same as `restart`. | `agent` |
| `restart` | Start a new attempt of the same visit from scratch, with the templates rendered at visit start. | `shell`, `python`, `set`, `condition`, `notify`, `subflow` (resumes the child run instead), `map` (continues with the next unfinished item) |
| `ask` | Turn the interruption into a human prompt: *"`<node>` was interrupted. Rerun, skip, or fail?"* with choices `rerun` (a new attempt, as `restart`), `skip` (record `succeeded` with empty results and route via `next`), `fail` (route to `fail`). The prompt is a `human_waiting` event with `kind: "resume"`, answered with `arcflow respond` like any other. | — |

  `human` and `sleep` nodes simply continue waiting (sleep for the remaining time).

- The interrupted attempt keeps its number and is recorded as `interrupted`; the new attempt counts toward `retry.max_attempts` only if it later fails.
- **At-least-once.** Side effects of an interrupted attempt (files written, commands run, commits made) are not undone. Flows with non-idempotent shell steps should set `on_resume: ask`. This is documented prominently.
- Workspaces are reused as recorded; if a recorded worktree is missing, resume fails with `E-WORKSPACE-MISSING` unless `--recreate-workspaces`.
- An attempt's normalized result (`result.json`) is written before its `attempt_finished` event. A visit whose last attempt finished but whose `visit_finished` was not written is completed from that result without running anything again; a visit that finished but whose `route_taken` was not written is routed from its recorded result.
- `--from` and `--force` record the jump as `route_taken` with `via: "resume"`. `--from` closes an interrupted visit as `interrupted` without rerunning it.
- `arcflow resume` exits 2 with `E-RESUME-REFUSED` when the run cannot be resumed as asked (a succeeded run, a failed or cancelled run without `--force`, an unknown `--from` node, a `--reload` that removes or retypes a visited node), 6 when the run is not found, and 7 with `E-LOCKED` when a live runner holds it.

### 7.6 Resume options

| Option | Effect |
|---|---|
| `--reload` | Use the **current** flow file instead of the snapshot. Allowed only if every node that has a finished visit still exists with the same type; the diff is recorded as `flow_reloaded` with both hashes, and the new snapshot is stored as `snapshot-<n>/`. |
| `--from <node>` | Continue at `<node>` (as if routed there), leaving earlier visits recorded. Implies nothing is rerun automatically. |
| `--rerun` | Rerun the interrupted visit from scratch regardless of `on_resume`: a new attempt with freshly rendered templates. |
| `--force` | Allow resuming a `failed` or `cancelled` run (recorded as `run_reopened`); it continues from the node that failed (a new visit) or with `--from`. A run that failed outside a node (a limit reached before a visit, an `outputs` error) needs `--from`. |
| `--due` | Resume only runs whose waiting human timeout or sleep has passed and that no runner holds: timed-out prompts are answered with their `default` (or time out), then a detached runner continues each run. Takes no run ID; meant for cron. `--json` gives `{resumed: [run IDs]}`. An `arcflow respond` that arrives after a deadline finds the timeout applied first and exits 6. |

### 7.7 Retention

Nothing is deleted automatically. `arcflow gc [--older-than 30d] [--status succeeded,cancelled] [--dry-run]` deletes finished run directories and their worktrees (§6.9 rules). `--older-than` counts from when the run finished and defaults to `retention.keep_days`; `--status` defaults to every finished status. Runs that are not finished (including `waiting`) or have a live lock are never collected.

---

## 8. Harness adapters

### 8.1 Contract

Adapters implement a Python protocol; the engine depends on nothing else (brief principle 9, ADR 0011). The engine is `asyncio`-based (the TUI's Textual is too), so adapters are `async`.

```python
class Adapter(Protocol):
    name: ClassVar[str]                       # "claude", "codex", "fake", or a custom name

    def capabilities(self) -> Capabilities: ...
    def options_schema(self) -> dict: ...     # JSON Schema for `harness_options`
    async def probe(self) -> Probe: ...       # binary found, version, tested range, auth hint
    async def run(self, req: AgentRequest, emit: Callable[[AdapterEvent], None]) -> AgentResult: ...
    def interactive_command(self, session_id: str, cwd: str) -> list[str] | None: ...
```

- **Resume cost.** `SessionSpec.previous_cost_usd` carries the running total the harness last reported for the session being resumed or forked, and `AgentResult.session_total_usd` returns the new total; the engine records totals per session (`attempt_finished`), so an adapter whose harness reports session totals (Claude) can report the call's own delta.
- Process adapters take `command` (the binary), `grace` and `tested_versions` from `harnesses.<name>` in config (§2.2).
- **`run`** starts the harness, emits normalized events while it runs, and returns the result. The brief's `start`/`stream`/`result` are this one coroutine; **cancellation** is `asyncio` task cancellation, on which the adapter must perform the stop sequence of §6.7 and then return (not raise) an `AgentResult` with outcome `cancelled` or `timed_out` as the engine instructs through `req.stop_reason`. Adapters built on `arcflow.adapters.ProcessAdapter` get the process-group handling for free.
- Adapters also declare `auth_env`, the environment variables their harness needs for authentication (§12.3). The engine keeps one adapter instance per runner process.
- **`emit`** must be called with `SessionStarted` **as soon as** the session ID is known (Claude: before spawning, since Arcflow chose it; Codex: on `thread.started`). The engine writes it to the event log and `fsync`s before anything else, so a crash after that point can resume the session.

```python
@dataclass
class Capabilities:
    structured_output: bool       # native schema-constrained output
    session_id: Literal["caller", "harness", "none"]
    resume: bool
    fork: bool
    interactive: bool             # interactive_command works for its sessions
    cost_usd: bool                # reports USD
    tokens: bool                  # reports token usage
    budget_cap: bool              # accepts a spend cap (backstop only)
    turn_cap: bool
    permission_profiles: frozenset[str]   # subset of {"read-only", "edit", "full"}
    tool_rules: bool              # honours allow_tools/deny_tools
    permission_hook: bool         # can route permission prompts to Arcflow (Future)
    streaming: bool               # emits progress during the run
    effort: bool                  # maps `effort`
    bare: bool                    # supports `bare`

@dataclass
class AgentRequest:
    run_id: str; node_id: str; visit: int; attempt: int
    prompt: str; instructions: str | None
    cwd: str; add_dirs: list[str]
    model: str | None; effort: str | None
    permissions: str; allow_tools: list[str]; deny_tools: list[str]
    output_schema: dict | None
    session: SessionSpec          # mode new|resume|fork, resume_id, new_id (caller-chosen, if supported)
    max_turns: int | None
    budget_usd: float | None; budget_tokens: int | None   # remaining allowance, backstop
    bare: bool
    env: dict[str, str]           # already filtered (§12.3)
    options: dict                 # harness_options, already validated
    attempt_dir: str              # where to write stream.jsonl etc.
    stop_reason: StopReason | None  # set by the engine before cancelling

AdapterEvent = SessionStarted(session_id) | Text(delta) | ToolCall(name, summary) \
             | ToolResult(name, ok, summary) | UsageUpdate(usage, cost_usd) \
             | PermissionDenied(tool, reason) | Log(level, message)
# UsageUpdate carries the attempt's cumulative usage so far.

@dataclass
class AgentResult:
    outcome: Outcome              # succeeded | failed | cancelled | timed_out | budget_exceeded | schema_invalid
    output: Any | None            # parsed structured output (validated again by the engine)
    text: str
    session_id: str | None
    usage: Usage; cost_usd: float | None
    num_turns: int | None; duration_s: float
    harness_version: str | None; model: str | None
    permission_denials: list[Denial]
    error: ErrorInfo | None       # kind, message, harness subtype / exit code / signal
    stopped_by: Literal[None, "sigint", "sigterm", "sigkill"]
```

**Adapters must:**

1. Judge the outcome from the final result event and schema validation, never from the exit code alone; a missing final result is `failed` with kind `no_result` (research §1.5).
2. Write the raw harness stream to `attempt_dir/stream.jsonl` unchanged.
3. Never inherit a parent agent's session: set session IDs explicitly where possible and use only `req.env` (§12.3).
4. Report the attempt's own cost and usage (deltas when resuming).
5. Deny nested orchestration by default (Claude: `--disallowedTools Workflow`) (ADR 0005).
6. Record the harness version (`system/init` for Claude, `codex --version` for Codex) and warn outside the tested range from config.

**Graceful degradation** when a capability is missing (engine behaviour):

| Missing | Engine does |
|---|---|
| `structured_output` | Prompt-based JSON with extraction and validation (§5.1.2). |
| `resume` | `session: continue/resume/fork` behave as `new`; `on_resume: resume` behaves as `restart`; `validate` warns. |
| `fork` | Falls back to `resume` with a warning. |
| `cost_usd` | Uses `prices` if configured, else token budgets only (§6.8). |
| `tokens` | Only wall-clock limits apply; `validate` warns for any node budget. |
| `budget_cap` / `turn_cap` | Arcflow-side enforcement only (always present anyway). |
| a permission profile | `E-PERMISSION-UNSUPPORTED` at validate time. |
| `interactive` | `handoff` nodes naming it fail validation. |

### 8.2 `harness_options`

Each adapter publishes a JSON Schema for its pass-through options; `validate` checks them. The escape hatch for anything else is `extra_args` (list of strings appended to the command line), which every process-based adapter accepts and which `validate` flags `I-EXTRA-ARGS` so reviewers notice.

### 8.3 Built-in adapters

**`claude`** (tier 1). Invocation (research §1.2), with the prompt on stdin:

```
claude -p [--bare] --session-id <uuid> \
  --output-format stream-json --verbose \
  [--json-schema <schema>] [--model M] [--effort E] [--max-turns N] [--max-budget-usd X] \
  <permission flags, §8.4> [--allowedTools …] --disallowedTools Workflow[,…] \
  [--append-system-prompt-file <instructions.md>] [--add-dir D …] \
  [--resume <id> [--fork-session]]
```

Capabilities: all true except `permission_hook` (Future). Maps result `subtype`/`terminal_reason` to outcomes (`error_max_budget_usd` → `budget_exceeded`, `error_during_execution` after Arcflow's SIGINT → `cancelled`/`timed_out` per `stop_reason`, other errors → `failed`). Requires `ANTHROPIC_API_KEY` for `bare: true`; otherwise uses whatever auth the Claude Code install has.

**`codex`** (tier 1). Invocation, checked against `codex exec --help` of codex-cli 0.147.0 (2026-09-29), with the prompt on stdin:

```
codex exec [resume <thread_id>] --json --skip-git-repo-check \
  -c sandbox_mode="<from profile>" -c approval_policy="never" \
  [-c sandbox_workspace_write.network_access=true] [-c sandbox_workspace_write.writable_roots=[…]] \
  [--ignore-user-config] [-p profile] [-m M] [-c model_reasoning_effort="E"] \
  [--output-schema <attempt_dir>/output-schema.json] -o <attempt_dir>/last-message.txt \
  [-C <cwd>] -
```

`exec resume` accepts no `--sandbox`, `-C` or `--add-dir`, so sandbox, approval and writable roots are always passed as `-c` overrides and the working directory is the process's (`-C` only for new threads). `codex exec` has no fork command, so the adapter declares no `fork`, and `session: {fork: …}` resumes instead (§5.1.3). Instructions are prepended to the prompt under an `# Instructions` heading. The harness version comes from `codex --version`. Codex's `input_tokens` already include `cached_input_tokens`.

Capabilities: `session_id: harness`, `fork: false`, `cost_usd: false` (prices from config), `budget_cap: false`, `turn_cap: false`, `tool_rules: false`. `harness_options` include `network: bool` (off by default, research §1.3), `profile`, `ignore_user_config` (default `true` for reproducibility), and `extra_args`. Behaviours still unverified (signals, exit codes, schema subset; research §7) are settled by the live contract tests (`tests/live`); until then the adapter treats any exit without `turn.completed` as `no_result`.

**`fake`** (tier 1). Deterministic, no processes, no cost; used by all engine tests (brief §16.3).

```yaml
harness: fake
harness_options:
  script: tests/fixtures/plan-script.yaml   # or inline `responses:`
```

```yaml
# plan-script.yaml: one entry per call, in order; `match` selects by node/visit/attempt instead
responses:
  - match: { node: plan }
    output: { status: done, steps: ["write parser", "write tests"] }
    text: "Plan ready."
    usage: { input_tokens: 1200, output_tokens: 300 }
    cost_usd: 0.02
    delay: 0.05s
  - match: { node: implement, visit: 1 }
    outcome: failed
    error: { kind: harness_error, message: "simulated crash" }
  - replay: fixtures/claude-2.1.285/success-schema.jsonl   # parse a recorded real stream
```

The fake adapter declares every capability, can simulate slow runs (for cancel/timeout tests) and can replay recorded streams through the real adapters' parsers. An entry with `match` answers every call it matches; the other entries answer each node's calls in order, counted per runner process, so tests that resume a run should use `match`.

### 8.4 Permission profiles

ADR 0005. Unattended default is `edit`, and anything that would prompt is denied.

| Profile | Claude Code | Codex |
|---|---|---|
| `read-only` | `--permission-mode dontAsk --permission-prompts none` (reads and pre-approved tools only; anything that would prompt is denied), plus `Edit`, `Write`, `NotebookEdit` in `--disallowedTools`; `allow_tools` adds pre-approved rules | `--sandbox read-only`, `approval_policy="never"` |
| `edit` | `--permission-mode acceptEdits --permission-prompts none`; Bash denied except rules in `allow_tools` | `--sandbox workspace-write`, `approval_policy="never"` |
| `full` | `--permission-mode bypassPermissions` | `--sandbox danger-full-access`, `approval_policy="never"` |

The Claude flags above follow the Claude Code 2.1.285 CLI reference and permission-mode docs (checked 2026-09-29); the live contract tests (`tests/live`) confirm them against a real install and are run by hand (§15 Q6); this table fixes the intent: `read-only` cannot change files, `edit` can change files in the workspace (and `add_dirs`) and run only explicitly allowed commands, `full` can do anything the user can. Every denial the harness reports appears in `permission_denials`.

### 8.5 Tier 2: command adapters

A YAML file `.arcflow/harnesses/<name>.yaml` defines an adapter for any CLI with JSON-lines output, without code (research §8.1):

```yaml
name: gemini
command: [gemini, -p, "{prompt}", --output-format, stream-json, -m, "{model}"]
prompt_via: argv            # argv | stdin | file ({prompt_file})
resume_command: [gemini, --resume, "{session_id}", -p, "{prompt}", --output-format, stream-json]
capabilities: { structured_output: false, resume: true, tokens: true, cost_usd: false }
stream:
  format: jsonl
  session_id: { when: 'event.type == "init"', value: event.session_id }
  text: { when: 'event.type == "message" and event.role == "assistant"', value: event.content }
  usage: { when: 'event.type == "result"', input_tokens: event.stats.input_tokens, output_tokens: event.stats.output_tokens }
  result: { when: 'event.type == "result"', success: 'event.status == "success"', text: event.response }
  error:  { when: 'event.type == "error"', message: event.message }
permissions:                # profile → extra argv
  read-only: [--approval-mode, plan]
  edit: [--approval-mode, auto_edit]
env: [GEMINI_API_KEY]       # auth variables to pass through
tested_versions: ">=0.9"
```

- Placeholders (`{prompt}`, `{prompt_file}`, `{schema_file}`, `{session_id}`, `{cwd}`, `{model}`, `{attempt_dir}`) are substituted **per argv element** (never through a shell). An element whose placeholder resolves to empty/None is dropped along with a preceding flag element when written as `["-m", "{model}"]` pairs (documented rule: an argv pair `[flag, "{x}"]` is omitted when `x` is unset).
- Mapping expressions use the §4 language with `event` bound to the parsed JSON line; `event` is the only name available.
- `name` must match the file name. `prompt_via` defaults to `stdin`; `{prompt}` is set only with `prompt_via: argv` and `{prompt_file}` only with `file`. The keys of `permissions` are the profiles the adapter supports (a node asking for another is `E-PERMISSION-UNSUPPORTED`). Optional keys: `interactive_command` (for `handoff`), `version_command` (default `[<command>, --version]`), `grace`, `usage.cached_input_tokens`, `usage.cost_usd`, `result.output`. `protocol: aap` marks an AAP executable (Planned). `arcflow schema harness` publishes the file's JSON Schema, and a flow's validation reports problems in the harness files it uses.
- Command adapters get the process handling, env filtering, JSON extraction and budget/timeouts of the engine for free.

### 8.6 Tier 3: plugin adapters

- **Python entry points.** A package declares `[project.entry-points."arcflow.adapters"] myharness = "mypkg.adapter:MyAdapter"`. Arcflow loads entry points lazily, only when a flow names that harness (keeps CLI start fast). Plugins are trusted code (§12.6).
- **Arcflow Adapter Protocol (AAP)** (Planned). An executable in any language, declared in `.arcflow/harnesses/<name>.yaml` with `protocol: aap` and `command: [...]`. JSON-lines over stdio:
  - Arcflow → adapter: `{"type":"hello","aap":1}`, `{"type":"run","request":{AgentRequest as JSON}}`, `{"type":"cancel","reason":"timeout"}`.
  - Adapter → Arcflow: `{"type":"hello","aap":1,"capabilities":{…},"version":"…"}`, `{"type":"event","event":{AdapterEvent}}`, `{"type":"result","result":{AgentResult}}`.
  - Exactly one `run` per process. Stderr is logged. Arcflow still owns the process group and the stop sequence.

### 8.7 Tier 4: ACP (Planned)

A generic adapter for agents speaking the Agent Client Protocol (`session/new`, `session/load`, `session/prompt`, `session/cancel`, `session/update`, `session/request_permission`). Configured as `harness: acp` with `harness_options: {command: [...]}`. Capabilities: no structured output, no cost or tokens (so only wall-clock limits and prompt-based JSON), `resume` when the agent supports `session/load`. Permission requests are answered by the profile (allow within `edit` scope, deny otherwise).

### 8.8 Conformance kit

`arcflow adapter test <name> [--live]` runs the published conformance suite against an adapter:

- **Offline** (default): feeds recorded fixture streams (success, structured output, schema failure, budget stop, SIGINT result, SIGTERM without result, auth failure, missing result) through the adapter's parser, or, for command/AAP adapters, through a stub executable that replays them, and checks the normalized results.
- **Live** (`--live`, opt-in, costs money): runs a small prompt set with a spend cap and checks session IDs, resume, schema output, cancel within the grace period, and env isolation.

Built-in adapters ship fixtures per tested harness version inside the package (`arcflow/conformance/fixtures/<harness>-<version>/`); a command adapter keeps its own in `.arcflow/harnesses/<name>/fixtures/`. Each set has one `<case>.jsonl` stream per case and an `expected.yaml` giving the normalized result each must produce (`outcome`, `error_kind`, `session_id`, `text`, `output`, `cost_usd`, `usage.*`, `permission_denials`). The command exits 0 when every case passes, 1 otherwise, 6 for an unknown adapter. A version bump in `tested_versions` requires re-recording them. The fixtures shipped for Claude Code 2.1.285 and Codex 0.147.0 are synthetic, written from the documented stream formats; the live suite is how they get replaced with recordings.

---

## 9. CLI

The CLI is the engine's scripting surface; the TUI calls the same library functions (brief principle 2). Commands are grouped by milestone need; everything marked Core ships in v1.

### 9.1 Conventions

- **`--json`** on every command prints exactly one JSON document to stdout: `{"ok": true, "data": …}` or `{"ok": false, "error": {"code": "E-…", "message": "…", "details": …}}`. Human-readable progress goes to stderr, so `--json` output is always parseable. `--events` (on `run`, `resume`, `logs --follow`) instead streams event JSON lines to stdout.
- The command is `arcflow`; the package also installs `arcf` as a short alias for the same entry point (ADR 0013).
- Global flags, accepted before or after the command name: `--project <dir>` (the project root, instead of discovering it), `--config <file>` (an extra config file, highest precedence), `--quiet` (no progress on stderr), `--verbose` (every event on stderr), `--no-color` (also `NO_COLOR`), `--as <name>` (who is acting: the responder of `respond` and `handoff`, the canceller of `cancel`; `respond --responder` and `cancel --by` override it for one command; the default is `$USER`).
- Run arguments accept the forms of §7.1 (`@last`, prefixes).
- The JSON shapes of every command are part of the public interface, published as JSON Schemas (`arcflow schema cli`, one envelope definition per command), and versioned with the format. The test suite validates real command output against them.

### 9.2 Exit codes

Stable and documented (ADR 0008):

| Code | Meaning |
|---|---|
| 0 | Success (the command worked; for `run`/`resume`/`wait`: the run succeeded) |
| 1 | The run failed |
| 2 | Usage error: bad arguments or an invalid `respond` answer |
| 3 | Flow or input invalid |
| 4 | The run is waiting for a human |
| 5 | The run was cancelled |
| 6 | Not found (flow, run, node) |
| 7 | Conflict: run locked by a live runner, or file changed on disk during an edit |
| 8 | The runner detached and left the run resumable (interrupted by a signal) |
| 70 | Internal error (a bug; the message asks for a report) |

### 9.3 Commands

| Command | Tier | Description |
|---|---|---|
| `arcflow init` | Core | Create `.arcflow/` with a commented `config.yaml` and a `.gitignore` for `runs/` and `worktrees/`, and `flows/hello.yaml`, an example that validates; existing files are kept. |
| `arcflow validate <flow>… [--strict]` | Core | Check files (§9.4). `--strict` turns warnings into errors. |
| `arcflow graph <flow> [--format ascii\|mermaid\|dot\|json]` | Core | Render the graph; `json` gives nodes and edges with conditions, for tools. |
| `arcflow run <flow> [--input k=v]… [--inputs-file f] [--workdir d] [--detach] [--on-wait prompt\|wait\|exit] [--events]` | Core | Create and start a run. Foreground by default. `--input k=@file` reads a value from a file. Prints the run ID first on stderr (and in `--json`). With `--json`, a succeeded run gives `{"ok": true, "data": {run_id, status, outputs, failure, totals}}`; any other end gives `ok: false` with error code `E-RUN-FAILED`, `E-RUN-CANCELLED`, `E-RUN-WAITING` or `E-RUN-DETACHED` and the same object as `details`. Invalid inputs are `E-INVALID-INPUT` and an invalid flow `E-INVALID-FLOW`, both exit 3. |
| `arcflow resume <run> [options of §7.6]` | Core | Continue a run. |
| `arcflow wait <run> [--timeout d]` | Core | Block until the run is terminal or waiting; exit code by status. For scripts that used `--detach`. When the timeout passes first it exits 8 (`E-TIMEOUT`): the run is still in progress. |
| `arcflow status [<run>]` | Core | One run in detail (current node, visits, pending human prompt, totals); without an argument, active runs. |
| `arcflow list [--flow X] [--status S] [--since d] [--limit n]` | Core | Runs, newest first. |
| `arcflow logs <run> [--node N] [--visit n] [--follow] [--raw] [--prompt]` | Core | Human-readable event log; `--raw` prints the harness stream; `--prompt` the rendered prompt. |
| `arcflow respond <run> [<node>] (--choice X \| --text T \| --ack) [--no-continue]` | Core | Answer a pending human node (node optional when only one is pending). |
| `arcflow cancel <run> [--reason T]` | Core | Cancel. |
| `arcflow flows` | Core | Flow files found under `flow_paths`, with name, description and validity. |
| `arcflow schema flow\|config\|cli\|harness` | Core | Print JSON Schemas. |
| `arcflow adapters [--probe]` | Core | Installed adapters (built-in, command adapter files, entry points) with their source and capabilities, and with `--probe` binary versions, whether they are in `tested_versions`, and auth hints. |
| `arcflow artifacts <run> [--node N]` | Core | List artifact files. |
| `arcflow tui [<flow>\|<run>]` | Core | Open the TUI (§10). |
| `arcflow gc` | Core | §7.7. |
| `arcflow doctor [<run>] [--truncate]` | Core | Check environment (Python, git, harness versions, pure-Python deps, a network filesystem under the runs directory); with a run, check and repair its event log and state: trim a torn last line, rebuild `state.json`, remove a stale lock and unfinished inbox files. A corrupt log is reported (exit 1); `--truncate` cuts it before its first bad line, keeping the old log as `events.jsonl.corrupt`. |
| `arcflow new <path> "<description>" [--harness h]` | Planned | Agent-authored flow (§11). |
| `arcflow edit <flow> "<instruction>" [--harness h] [--yes]` | Planned | Agent edit of an existing flow (§11). |
| `arcflow flow <op> <flow> …` | Core | Structured edits (§10.4): `add-node`, `rm-node`, `rename-node`, `set`, `unset`, `connect`, `disconnect`. |
| `arcflow handoff <run> [<node>]` | Core | Open a pending handoff session interactively. |
| `arcflow adapter test <name> [--live]` | Core | Conformance kit (§8.8). |

**Inspection output.** `status` and `list` describe a run as `{run_id, flow, status, current, pending, created_at, started_at, finished_at, parent, totals: {usd_spent, tokens_spent, steps, active_s}, failure}`, with `status` the display status (§1.1). `status <run>` adds `inputs`, `workdir`, `visits`, `nodes` (`{outcome, visit, error}` per node), `in_progress`, `pending_human`, `outputs` and `vars`; `status` alone lists the active runs (`pending`, `running`, `waiting`, `interrupted`). `list --status` takes a comma-separated list and `--since` a duration. `logs --events` (or `--json`) prints events as JSON lines; `--raw` and `--prompt` need `--node`. `artifacts` gives `{node, visit, path, size}` entries and `flows` `{file, name, description, valid, errors, warnings}` for every YAML file under `flow_paths` that has a `nodes` key. `graph --format json` gives `{name, start, nodes: [{id, type, description}], edges: [{from, to, via, case_index, when, explicit}]}`, leaving out the implicit `on_error: fail` edges. Commands that read a waiting run (`status`, `logs`, `wait`, `artifacts`, `respond`) first apply any passed human deadline (§6.11).

### 9.4 `arcflow validate`

Checks, in order, stopping at the first stage with errors: YAML syntax; schema (types, unknown keys, required keys); identifiers and reserved words; templates (`extends` resolution, cycles); referenced files exist (`prompt_file`, schemas, subflows, `python` modules are *not* imported); JSON Schemas valid; graph (targets exist, `start` exists, reachability from `start` (`W-UNREACHABLE`), a path to `end` or `fail` from every node (`E-NO-EXIT`), default routes, unguarded cycles); expressions parse and references resolve (§4.5); harness known and `harness_options` valid; capability checks (§8.1); security lints (§12). Each problem has a stable code (Appendix C), a severity, a message, the file, line and column (from `ruamel.yaml` positions) and a JSON pointer:

```
flows/implement-feature.yaml:41:13 error E-UNKNOWN-TARGET  next[1].to: no node named "implment" (did you mean "implement"?)
```

With `--json`, problems are an array of `{code, severity, message, file, line, column, pointer, hint}`, the format authoring agents consume (§11). `pointer` is an RFC 6901 JSON pointer into the file; the text output shows it as a dotted path (`nodes.test.next[1].to`). When every file is valid the document is `{"ok": true, "data": {"files": […], "problems": […]}}` (problems then holds only warnings and infos); otherwise it is `{"ok": false, "error": {"code": "E-INVALID-FLOW", "message": …, "details": {"files": […], "problems": […]}}}` and the exit code is 3. Each `files` entry is `{file, valid, name}`.

While v1 is being built, the last stage reports `E-NOT-IMPLEMENTED` for node types the runner cannot execute yet (`docs/milestones.md`).

---

## 10. TUI, live graph and editing

The TUI is a Textual application in the same package. It is a client: it reads flow files and run directories, and acts only through the library functions the CLI uses (lock and inbox rules of §7.4 apply). It holds no flow state that is not in the file (brief principle 1); layout, selection and scroll position are view state only and are never written anywhere.

### 10.1 Screens (Core unless marked)

| Screen | Content | Actions |
|---|---|---|
| **Flows** | Flow files under `flow_paths`: name, description, validity badge, last run status. | validate, run (input form built from `inputs`), open graph, open in `$EDITOR` |
| **Runs** | Active and recent runs: status (incl. `interrupted`), flow, current node, duration, spend, pending prompts first. Filter by flow and status. | open, resume, cancel, respond |
| **Run detail** | Graph (§10.2) with live node status; timeline of visits; inspector for the selected visit: rendered prompt, output (pretty JSON), streamed agent activity, stdout/stderr, artifacts, usage and cost; budget and limit gauges. | respond inline to pending human nodes, cancel, resume, open handoff, open artifact (in `$VISUAL`/`$EDITOR`), copy session ID, "open in harness" (`claude --resume …` in the same terminal, with the TUI suspended until it exits) |
| **Flow graph / editor** | The flow's graph, live-synced with the file (§10.3), plus an inspector form for the selected node. | structured edits (§10.4), "edit with agent" (§11, Planned) |

Human prompts: a pending prompt shows as a banner in every screen and as a modal in run detail with the message, `show` lines, choices, and a text field when allowed. Answering writes through `respond`.

`arcflow tui` opens on Runs; given a flow file it opens that flow's graph, given a run (ID, prefix, `@last`) that run's detail. Keys: `r` runs, `f` flows, `enter` open, `s` cycle the runs' status filter, `escape` back, `q` quit, `ctrl+p` the command palette; on runs and run detail `a` answer the pending prompt, `c` cancel, `u` resume (a detached runner); on run detail `h` open a pending handoff, `g` open the selected visit's session in its harness, `y` copy its session ID, `o` open its first artifact; on flows `x` run (a form built from `inputs`, starting a detached run) and `v` validate. Every action goes through the same library functions as the CLI and records the same events (`via: "tui"` for answers). Screens refresh from disk (runs every second, run detail every half second, a shown flow file every 300 ms), so a run driven by another process updates live. Node status in the graph is a marker (`✓` succeeded, `✗` failed, `▶` running, `…` waiting, `‖` interrupted, `⊘` cancelled, `⌛` timed out) plus colour, with `×n` for repeated visits.

### 10.2 Graph rendering

- Layered (Sugiyama) layout with `grandalf` (pure Python), drawn with box-drawing characters: nodes as boxes labelled `id` and type icon, forward edges downward, back edges (loops) routed on the side in a distinct style, edge labels showing a shortened `when`.
- Node status colours in run detail: not visited, running (animated), succeeded, failed, waiting, skipped path; visit counts as a badge (`×3`).
- Graphs larger than the viewport pan and zoom (two zoom levels: full boxes, compact dots). Above 60 nodes, or when layout takes more than 200 ms, the view falls back to a vertical list in topological order with edges shown as "→ target" lines (research §4).
- `arcflow graph --format ascii` (the default format) uses the same renderer, so the CLI and TUI never disagree. Boxes show a type icon (◆ agent, $ shell, ? condition, ☺ human, ◷ sleep, = set, λ python, ⊂ subflow, ∀ map, ↪ handoff, ✉ notify, ● end, ✖ fail) and an optional status marker; `on_error` edges are dashed; `on_error: continue` draws no separate edges, since it routes with `next`. `when` labels are shortened to 18 characters and placed where they fit, truncated further when space is tight. The list fallback shows each node with its edges as `→ target  if <when>` lines. The renderer reports each node's box position, so the TUI can colour it by status.

### 10.3 Live two-way sync with the file

Alexey's requirement: one YAML file edited equally from the TUI, by agents and by hand, visible live everywhere (ADR 0002).

- The TUI **polls** the open flow file (and its referenced prompt/schema files) every 300 ms by `mtime`, size and, on change, SHA-256. No native watcher (ADR 0001).
- On a change: parse and validate. **Valid** → re-render the graph in place, keeping selection by node ID and highlighting nodes whose definition changed for 2 s. **Invalid** → keep showing the last valid graph, dimmed, with the problems listed (clickable to line numbers) until the file is valid again.
- The TUI's own edits are written by the editing library (§10.4), then read back through the same polling path, so there is one code path for "the file changed" regardless of who changed it.

### 10.4 Structured edits

All edits, from the TUI or from `arcflow flow <op>`, go through one library, `arcflow.edit`, which:

1. Reads the file with `ruamel.yaml` round-trip mode and records its SHA-256.
2. Applies the operation to the round-trip tree, preserving comments, key order, quoting and block styles of everything it does not touch.
3. Validates the result; an edit that makes a valid file invalid is refused (the TUI shows why), except in the free-text field editor where the user explicitly saves an invalid draft.
4. Before writing, re-reads the file; if its hash changed since step 1, it **reloads and asks** (TUI) or exits with code 7 (CLI) rather than overwrite (ADR 0002).
5. Writes atomically (temp file in the same directory, `fsync`, rename), keeping the file mode.

Operations:

| Operation | Behaviour |
|---|---|
| add node | Insert under `nodes` after the selected node with a type template; opens its form. |
| remove node | Refuses while other nodes route to it, unless "also remove edges" is chosen. |
| rename node | Renames the key **and** every reference: `next`/`on_error` targets, `start`, `session.resume/fork`, `handoff.from`, and expressions/templates (rewritten through the expression AST, so `nodes.old.x`, `visits.old`, `nodes["old"]` are all updated; text outside `${{ }}` is untouched). |
| set / unset field | Via the node's form, generated from the node type's schema; prompts open in a multi-line editor or `$EDITOR`. |
| connect / disconnect / reorder cases | Edits `next`/`on_error` lists; conditions are edited as text with live parse feedback. |
| change type | Keeps common keys, drops keys the new type does not accept after confirmation. |

Only the lines an edit changes are rewritten: the library dumps the original and the edited tree in the same normalized style, diffs the two, and applies only the changed hunks to the original text, so untouched lines keep their exact formatting (spacing inside flow mappings included) and changed lines take the file's detected indentation. Blank lines that separate a block from the next stay in place when the block's last line changes. A new node starts with a minimal body for its type (`add-node --set key=value` adds fields). Removing a node with `--remove-edges` also removes the cases that route to it, and a single `next`/`on_error` naming it (which then falls back to its default).

`arcflow flow` exits 0 after a write, 3 (`E-EDIT-REFUSED`, with the problems) when the edit would make a valid flow invalid, 2 when the operation itself does not apply (an unknown node, a node still routed to), 6 when the file does not exist, and 7 (`E-CONFLICT`) when the file changed on disk since it was read. Its `--json` data is `{file, problems}` (the warnings left after the edit). Operation syntax: `add-node <flow> <id> --type T [--after N] [--set k=v]…`, `rm-node <flow> <id> [--remove-edges]`, `rename-node <flow> <old> <new>`, `set <flow> <node> <path> <yaml value>`, `unset <flow> <node> <path>`, `connect <flow> <from> <to> [--when E] [--on-error] [--position n]`, `disconnect <flow> <from> <to> [--on-error]`.

Positions are never stored: the layout is recomputed, so a hand edit and a TUI edit produce the same picture. Free-form drag-and-drop layout remains a non-goal.

**Editing while runs are active.** Runs use their snapshot (§6.1), so editing is always safe. Run detail shows "flow changed since this run started" with a diff, and offers `resume --reload` only when it is allowed (§7.6).

### 10.5 Keys and accessibility

Every action has a key binding and appears in a command palette (`ctrl+p`); mouse is optional. Colours come from a theme with a monochrome fallback, and status is never conveyed by colour alone (icons and text too). Minimum terminal size 80×24; smaller shows a warning.

---

## 11. Agent-authored flows (Planned)

Flows are data, so agents can author and edit them with the same safety as a person (research §8.2).

**`arcflow new <path> "<description>"`** runs a built-in flow shipped inside Arcflow (`builtin:author-flow`, itself a normal flow file, so Arcflow dogfoods its own format). Its agent node:

- gets as instructions a bundled authoring guide: the flow JSON Schema, the node catalog (§5, generated from the same source as the schema), the expression function list, the example flows from Appendix A, the project's existing flows and schemas list, and the installed adapters with capabilities;
- has permission profile `edit` limited to writing `<path>` and new files under the flow's directory (prompt and schema files), plus running `arcflow validate --json <path>`;
- loops: write → validate → fix, up to 5 validation rounds (a guarded cycle in the built-in flow), then ends with a human node showing the file and the final validation result: `accept`, `revise` (asks for feedback text and loops), `discard`.

**`arcflow edit <flow> "<instruction>"`** and the TUI's **"Edit with agent"** do the same against an existing file: Arcflow snapshots the file, the agent edits it in place (the TUI graph updates live as it saves, §10.3), and at the end the person sees a diff and chooses accept or revert. Revert restores the snapshot byte for byte. If the person also edited the file during the agent's run, Arcflow shows a three-way view and never silently drops either change.

**Never run automatically.** Neither command runs the resulting flow. A flow written by an agent is reviewed like code (§12.1).

Authoring agents can also be used outside Arcflow: `arcflow schema flow` plus `arcflow validate --json` is the whole interface an external agent needs.

---

## 12. Security

### 12.1 Threat model

- **A flow file is code.** It runs shell commands and agents with the user's privileges. Running an untrusted flow is like running an untrusted Makefile. Arcflow does not sandbox shell or python nodes. The docs say this plainly, and `arcflow run` of a flow file that is not tracked by git or was modified by `arcflow new/edit` in the last session prints a one-line notice (not a prompt).
- **Agent output is untrusted input.** Agents read repositories, issues and web pages that may contain prompt injections, and their outputs flow into later prompts, shell commands and human prompts. Arcflow's defences: permission profiles (§8.4), safe interpolation rules (§12.5), structured outputs validated against schemas, and human nodes before risky steps (a documented pattern, and a lint, §12.7).
- **Out of scope:** multi-user isolation, protecting run directories from the local user, authenticating responders (`responder` is informational: anyone who can write the run directory can answer).

### 12.2 Agent autonomy

- Default profile `edit`, prompts auto-denied (ADR 0005). Nested orchestration denied (Claude `Workflow` tool).
- `permissions: full` produces a validate warning (`W-FULL-PERMISSIONS`) and `arcflow run` refuses it (`E-FULL-PERMISSIONS`, exit 3) unless the flow is run with `--allow-full` or the project config sets `allow_full: true`.
- Permission denials are recorded per visit and can be routed on.

### 12.3 Environment control

Harness and command processes start from an **empty environment** plus:

1. a base allowlist: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TERM`, `TZ`, `TMPDIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (and lower-case forms), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `GIT_*` except `GIT_DIR`/`GIT_WORK_TREE`;
2. auth variables each adapter declares (`claude`: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`, cloud-provider variables for Bedrock/Vertex; `codex`: `CODEX_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`);
3. `env_passthrough` from config;
4. the flow's and node's `env` (templated);
5. `ARCFLOW_*` run variables.

Always removed, even if listed above: `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AUTO_BACKGROUND_TASKS`, `CODEX_THREAD_ID`, `CODEX_SANDBOX*`, and any other variable matching the adapters' declared session denylist (research §1.4, §1.5.4). The denylist is refined in M3 (§15 Q7).

Templates see `env.X` only for variables allowed by 1–3.

### 12.4 Secrets and logs

- Arcflow never writes environment values to events, `visit.json` or logs; `env` entries are recorded by name only.
- Rendered prompts and harness streams are stored as is, because they are the audit trail. They can contain secrets that agents printed; run directories are `0700`, and `.arcflow/.gitignore` excludes `runs/` and `worktrees/`.
- `redact` patterns from config are applied to everything Arcflow writes itself (events, `stdout.log`, `stderr.log`, TUI display). Raw `stream.jsonl` is redacted too, once its attempt ends, unless `redact_streams: false` (keeping byte-exact fixtures for adapter debugging).

### 12.5 Injection-safe interpolation

- `shell.run` is a shell script: interpolating agent output into it can execute attacker-chosen commands. `arcflow validate` warns (`W-SHELL-INTERPOLATION`) whenever `run` contains `${{ }}` that references `nodes.*` or `vars.*` without wrapping it in `shq(…)`. The recommended forms are `args:` (no shell) or passing values through `env:` and referring to `"$VAR"` in the script.
- Human `message` rendering in the TUI escapes Textual markup; terminal control sequences in any displayed agent text are stripped.
- Command adapters substitute placeholders per argv element, never through a shell (§8.5).
- `on_wait` hooks receive values through `ARCFLOW_*` environment variables, not interpolation.

### 12.6 Extensions

Entry-point plugins, `python` nodes and command adapters run with full user privileges; installing one is trusting it. `arcflow adapters` shows each adapter's source (built-in, file path, or package and version).

### 12.7 Lints

`W-SHELL-INTERPOLATION`, `W-FULL-PERMISSIONS`, `W-UNBOUNDED`, `W-UNGUARDED-CYCLE`, and `W-NO-HUMAN-BEFORE-RISKY` (a shell node whose `run`/`args` matches configurable risky patterns such as `git push`, `gh pr merge`, `kubectl`, `terraform apply`, `rm -rf`, reachable without a human node on every path to it). Lints are warnings; `--strict` makes them errors (a CI recipe).

---

## 13. Testing strategy

Brief §16: tests first; engine tests use the fake adapter; unit tests never call real harnesses or spend money.

| Layer | What | How |
|---|---|---|
| Parser and validator | Every rule in §3–§5 and every code in Appendix C | A corpus `tests/flows/invalid/*.yaml`, each file annotated with its expected codes and positions (`# expect: E-UNKNOWN-TARGET @ 41:13`); `tests/flows/valid/*.yaml` including all Appendix A examples. The M1 acceptance criterion is this corpus. |
| Expressions | Grammar, forbidden constructs, null-safety, functions, rendering rules | Table-driven tests plus property tests (Hypothesis, a dev-only dependency) that no accepted expression can reach builtins or dunders. |
| Engine | Routing, loops, limits, retries, `on_error`, budgets, timeouts, cancellation, human waits, workspaces | Fake adapter scripts; assertions on the resulting **event log** (golden files, normalized for timestamps and IDs). Time is injected (fake clock) so sleeps and timeouts run instantly. |
| Crash and resume | At-least-once guarantees, `on_resume` modes, torn last line, stale locks | A fault-injection hook (`ARCFLOW_TEST_CRASH_AT=<event type>:<n>`) that kills the runner right after the n-th event of that type in the run's log (counting events earlier processes wrote), and `ARCFLOW_TEST_CRASH_CHILD_AT` doing the same inside child runs; for each example flow, crash at every event boundary, resume, and assert the final state equals the uncrashed run and no finished visit ran twice. |
| Adapters (offline) | Stream parsing and outcome mapping for `claude`, `codex`, command adapters | Recorded fixtures per pinned harness version (§8.8). |
| Adapters (live) | Real harness smoke tests | Marked `live`, excluded by default, run manually or in a scheduled CI job with a spend cap (`--max-budget-usd` plus Arcflow's budget) and secrets; never on pull requests from forks. |
| CLI | Every command's `--json` shape and exit code | Snapshot tests against the published CLI schemas. |
| TUI | Screens render and actions work | Textual's `App.run_test()` pilot and SVG snapshot tests, against run directories produced by fake-adapter runs. |
| Round-trip editing | Comments, order and formatting preserved; rename rewrites all references | For a corpus of hand-formatted flows: apply each operation and assert that the diff touches only the expected lines; round-trip with no operation is byte-identical. |
| Packaging | Pure-Python rule (ADR 0001) | CI builds the wheel and zipapp and fails if any runtime dependency (transitively) ships a non-`py3-none-any` wheel; install tests on Python 3.10–3.13, Linux and macOS. |

Dogfooding (brief §15, after M4): Arcflow's own build loop runs as a Arcflow flow, which serves as a long-running integration test.

---

## 14. Non-goals (v1)

- Calling model APIs directly or implementing an agent loop.
- A web UI, hosted service, or long-running daemon; built-in scheduling (ADR 0008).
- Parallel execution of nodes (`parallel`/`join`, concurrent `map`) (ADR 0007). The event log and state are ready for it.
- A code-based flow builder or any second flow representation (ADR 0002).
- Free-form visual layout (drag and drop, stored positions). Structured editing of the file is in scope for v1 and agent-driven editing is Planned, which revises the brief's "visual graph editing" non-goal.
- `include` of anything other than templates (§3.7).
- Multi-user collaboration, remote runners, or authenticated responders.
- Routing harness permission prompts to human nodes (Future; ADR 0005).
- Windows (§15 Q10).
- Built-in integrations (Slack, email, GitHub) beyond the `on_wait` hook and `notify` command/webhook.

---

## 15. Review decisions

Draft 1 left these questions open, each with a default. Alexey's review on 2026-09-29 resolved all of them; the spec text above reflects the resolutions. A later change to any of them needs an ADR.

| # | Question | Resolution |
|---|---|---|
| Q1 | ADR 0003 names a node field `status`, which collides with the `status` field agents are encouraged to put in their structured output. | The field is **`outcome`** (§4.3); ADR 0003 carries an amendment note. |
| Q2 | Accept `true`/`false`/`null` in expressions next to Python's `True`/`False`/`None`? | Both spellings are accepted (§4.1). |
| Q3 | ADR 0002 lists `include` of "shared fragments"; should it be limited to templates so the graph stays in one file? | Templates only (§3.7); ADR 0002 carries an amendment note. |
| Q4 | Which features beyond the brief's M0–M8 belong in v1? | ADR 0014: v1 adds structured editing (§10.4, TUI editor, `arcflow flow`), the `set`, `python`, `subflow`, `map`, `handoff` and `notify` nodes, `include`, command adapters (§8.5) and the conformance kit (§8.8). Agent authoring (§11), AAP (§8.6) and ACP (§8.7) stay Planned. |
| Q5 | Default run budget. | `usd: 25`, `tokens: 10_000_000` (§3.4). |
| Q6 | Exact Claude flag set per permission profile. | Intent fixed in §8.4; the flags are fixed by contract tests against the pinned harness versions, and §8.4 is updated from them. |
| Q7 | Full env denylist for nested agent sessions (research §7.6). | The list in §12.3, refined by the same contract tests. |
| Q8 | Codex behaviours: signals, exit codes, default sandbox, schema subset (research §7.1–7.4). | Treated conservatively (§8.3) until the contract tests decide. |
| Q9 | Human node timeout default. | 7 days, with lazy enforcement and `arcflow resume --due` (§6.11). |
| Q10 | Windows support. | Not in v1; Linux and macOS only. Process-group code stays behind one module so Windows (job objects) can be added. |
| Q11 | Does `respond` auto-start a detached runner? | Yes, unless `--no-continue` (§6.11). |
| Q12 | Price table for harnesses that report tokens only. | Config only; no built-in prices (§2.2, §6.8). |
| Q13 | Hands-on lionagi trial (research §2.2). | Skipped; not expected to change the design. |
| Q14 | Minimum Python version. | 3.10, per ADR 0001. |
| Q15 | Interpreter for `shell.run`. | `bash -eo pipefail` when bash exists, else `sh -e`; override with `shell:` (§5.2). |

---

## 16. Traceability to ADRs

| ADR | Where this spec implements it |
|---|---|
| 0001 Python, pure-Python deps | §8.1 (asyncio adapters), §10.2 (`grandalf`), §10.3 (polling), §13 (packaging check) |
| 0002 One YAML file, expressions | §3, §4, §10.3–10.4, §11 |
| 0003 State and addressing | §4.3 (with Q1) |
| 0004 Workspace isolation | §6.9 |
| 0005 Permissions | §5.1, §8.4, §12.2 |
| 0006 Human node delivery | §5.4, §6.11 |
| 0007 No parallelism | §5.9, §7.3 (`branch` key), §14 |
| 0008 External scheduling | §6.11, §9.2, Appendix A example 2 |
| 0009 Name deferred | Superseded by 0013 |
| 0013 Name: Arcflow | Header note; §9.1 (`arcf` alias) |
| 0010 MIT | No spec impact |
| 0011 Adapters | §8 |
| 0012 Run storage and resume | §7 |
| 0014 v1 scope | Tiers (header), §5 node catalog, §9.3, §10, §15 Q4 |

---

## Appendix A. Example flows

All four are part of the valid-flow test corpus (§13). The first three use only the five basic node types; the fourth uses composition and custom logic.

### A.1 `implement-feature`: plan, implement, test loop, approval

The brief's example in final syntax. Shows structured output with a `status` field, `session: continue` in a fix loop, the `on_error: continue` test pattern, visit guards, a human approval, and `to: fail`.

```yaml
arcflow: 1
name: implement-feature
description: Plan with Claude, implement with Codex, loop on tests, ask before merging.

inputs:
  feature:
    type: string
    required: true
    description: What to build, in one or two sentences.
  branch:
    type: string
    required: true
  test_command:
    type: string
    default: npm test

defaults:
  max_visits: 5
  agent:
    timeout: 30m

limits:
  budget: { usd: 10 }

nodes:
  plan:
    type: agent
    harness: claude
    model: sonnet
    permissions: read-only
    prompt: |
      Write an implementation plan for: ${{ inputs.feature }}
      Read the relevant code first. Do not change any files.
      Set status to "blocked" if the request is unclear, and explain why in summary.
    output_schema: schemas/plan.json
    next:
      - when: nodes.plan.output.status == "ready"
        to: implement
      - to: fail
        reason: "Planner is blocked: ${{ nodes.plan.output.summary }}"

  implement:
    type: agent
    harness: codex
    session: continue            # later visits keep the context of earlier attempts
    prompt: |
      ${{ "Implement this plan:" if visits.implement == 1 else "The tests still fail. Fix the code, not the tests." }}

      ${{ nodes.plan.output if visits.implement == 1 else tail(nodes.test.stdout + nodes.test.stderr, 120) }}
    next: test

  test:
    type: shell
    run: ${{ inputs.test_command }}
    timeout: 15m
    on_error: continue           # a failing test run is data, not a run failure
    next:
      - when: nodes.test.exit_code == 0
        to: approve
      - when: visits.implement < 3
        to: implement
      - to: escalate

  approve:
    type: human
    message: |
      Tests pass for "${{ inputs.feature }}". Merge ${{ inputs.branch }}?
    show:
      - "Plan: ${{ nodes.plan.output.summary }}"
      - "Estimated spend so far (USD): ${{ round(run.budget.usd_spent, 2) }}"
    choices: [merge, reject]
    timeout: 3d
    default: reject
    next:
      - when: nodes.approve.choice == "merge"
        to: merge
      - to: end

  merge:
    type: shell
    args: [git, merge, --no-ff, "${{ inputs.branch }}"]

  escalate:
    type: human
    message: Tests still fail after 3 attempts. Fix it by hand, then choose "retry" to run the tests again.
    choices: [retry, give_up]
    next:
      - when: nodes.escalate.choice == "retry"
        to: test
      - to: fail
        reason: Gave up after repeated test failures.
```

`schemas/plan.json`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "steps"],
  "properties": {
    "status": { "enum": ["ready", "blocked"] },
    "summary": { "type": "string" },
    "steps": { "type": "array", "items": { "type": "string" } }
  }
}
```

### A.2 `nightly-deps`: scheduled upgrade in a worktree with cross-harness review

Run from cron, CI or a systemd timer (ADR 0008): `arcflow run flows/nightly-deps.yaml --on-wait exit --json`. Shows `output: json`, `ok_codes`, templates, a named worktree shared by several nodes, a reviewer on a different harness, `args` for safe interpolation, and a fully unattended design: instead of waiting on a person, it opens a PR that a person reviews.

```yaml
arcflow: 1
name: nightly-deps
description: Upgrade outdated npm dependencies in a worktree, test, review with a second agent, open a PR.

limits:
  max_duration: 2h
  budget: { usd: 8, tokens: 4_000_000 }

templates:
  in_upgrade_tree:
    workspace:
      worktree: upgrade
      branch: deps/${{ run.id }}
      keep: true
  implementer:
    extends: in_upgrade_tree
    type: agent
    harness: claude
    permissions: edit
    allow_tools: ["Bash(npm install:*)", "Bash(npm test:*)", "Bash(npx tsc:*)"]
    timeout: 40m

nodes:
  outdated:
    type: shell
    run: npm outdated --json
    ok_codes: [0, 1]             # npm exits 1 when something is outdated
    output: json
    next:
      - when: len(nodes.outdated.output) == 0
        to: end
      - to: upgrade

  upgrade:
    extends: implementer
    session: continue
    prompt: |
      ${{ "Upgrade these packages to their latest versions, adapting code to breaking changes:" if visits.upgrade == 1 else "Your last change needs more work." }}
      ${{ nodes.outdated.output if visits.upgrade == 1 else "" }}
      ${{ "Test failures:\n" + tail(nodes.test.stderr, 80) if nodes.test and nodes.test.exit_code != 0 else "" }}
      ${{ "Reviewer issues:\n" + join(nodes.review.output.issues, "\n") if nodes.review and nodes.review.output.verdict == "changes" else "" }}
      Run the tests before you finish. Report status "done" only if they pass.
    output_schema:
      type: object
      additionalProperties: false
      required: [status, summary]
      properties:
        status: { enum: [done, blocked] }
        summary: { type: string }
    next:
      - when: nodes.upgrade.output.status == "done"
        to: test
      - to: fail
        reason: "Upgrade blocked: ${{ nodes.upgrade.output.summary }}"

  test:
    extends: in_upgrade_tree
    type: shell
    run: npm ci && npm test
    timeout: 20m
    on_error: continue
    next:
      - when: nodes.test.exit_code == 0
        to: review
      - when: visits.upgrade < 3
        to: upgrade
      - to: fail
        reason: Tests fail after three upgrade attempts.

  review:
    extends: in_upgrade_tree
    type: agent
    harness: codex
    permissions: read-only
    prompt: |
      Review the uncommitted dependency upgrade in this repository for risky changes,
      missed breaking changes and unnecessary edits. Approve only if it is safe to merge.
    output_schema: schemas/review.json     # {verdict: approve|changes, issues: [string]}
    next:
      - when: nodes.review.output.verdict == "approve"
        to: open_pr
      - when: visits.upgrade < 3
        to: upgrade
      - to: fail
        reason: Reviewer still requests changes after three rounds.

  open_pr:
    extends: in_upgrade_tree
    type: shell
    env:
      PR_BODY: ${{ nodes.upgrade.output.summary }}
    run: |
      git add -A
      git commit -m "chore(deps): nightly upgrade"
      git push -u origin HEAD
      gh pr create --fill --title "chore(deps): nightly upgrade" --body "$PR_BODY"
```

`open_pr` passes agent text through `env` rather than interpolating it into the script (§12.5), so `validate` raises no `W-SHELL-INTERPOLATION`. It will raise `W-NO-HUMAN-BEFORE-RISKY` for `git push` unless the project config excludes it; that is the intended prompt to think about it (here, the PR is the human checkpoint).

### A.3 `babysit-pr`: poll CI, fix failures, ask when stuck

Shows `sleep` in a polling loop bounded by `max_visits`, JSON from a CLI, token budgets for a harness without USD, an agent `status` of `blocked` routed to a person, and a free-text human answer fed back to the agent.

```yaml
arcflow: 1
name: babysit-pr
description: Watch a pull request's CI; when it fails, have an agent fix it; ask a person when the agent is stuck.

inputs:
  pr: { type: integer, required: true }

limits:
  max_duration: 6h
  budget: { usd: none, tokens: 3_000_000 }   # Codex reports tokens, not USD

nodes:
  checkout:
    type: shell
    args: [gh, pr, checkout, "${{ inputs.pr }}"]
    next: wait

  wait:
    type: sleep
    duration: ${{ "1m" if visits.wait == 1 else "10m" }}
    max_visits: 30
    next: checks

  checks:
    type: shell
    args: [gh, pr, checks, "${{ inputs.pr }}", --json, "name,bucket,link"]
    ok_codes: [0, 1, 8]          # gh: 1 = some failed, 8 = some pending
    output: json
    max_visits: 30
    next:
      - when: '"fail" in pluck(nodes.checks.output, "bucket")'
        to: fix
      - when: '"pending" in pluck(nodes.checks.output, "bucket")'
        to: wait
      - to: end

  fix:
    type: agent
    harness: codex
    session: continue
    max_visits: 4
    prompt: |
      CI failed on this branch. Failed checks:
      ${{ nodes.checks.output }}
      ${{ "Guidance from the maintainer: " + nodes.ask.text if nodes.ask else "" }}
      Use `gh run view --log-failed` to read the logs. Fix the cause, commit and push.
      If you cannot fix it without a decision from a person, set status to "blocked" and ask your question in summary.
    permissions: edit
    harness_options:
      network: true                # pushing needs network (§8.3)
    output_schema: schemas/fix.json    # {status: fixed|blocked, summary}
    next:
      - when: nodes.fix.output.status == "fixed"
        to: wait
      - to: ask

  ask:
    type: human
    message: |
      The agent is stuck on PR #${{ inputs.pr }}:
      ${{ nodes.fix.output.summary }}
    input: text
    timeout: 1d                  # no default, so a timeout is an error and on_error (fail) ends the run
    next: fix
```

### A.4 `triage-issues`: composition and custom logic

Shows `include`, `python`, `map` over a subflow, `set` and `notify`. It is in the valid corpus from M1 and becomes runnable once those node types land (`docs/milestones.md`).

```yaml
arcflow: 1
name: triage-issues
description: Label and answer new GitHub issues, one subflow run per issue.

include:
  - shared/templates.yaml          # provides the `triager` agent template

inputs:
  since: { type: string, required: true, description: "ISO date, e.g. 2026-09-29" }

nodes:
  fetch:
    type: shell
    args: [gh, issue, list, --state, open, --json, "number,title,body,labels", --search, "created:>=${{ inputs.since }}"]
    output: json
    next: select

  select:
    type: python
    call: tools.triage:needs_triage     # returns the issues without a triage label
    args: { issues: "${{ nodes.fetch.output }}" }
    next: each

  each:
    type: map
    items: ${{ nodes.select.output }}
    flow: triage-one.yaml
    inputs:
      issue: ${{ item }}
    on_item_error: continue
    max_items: 30
    next: tally

  tally:
    type: set
    vars:
      triaged: ${{ nodes.each.succeeded }}
      failed: ${{ nodes.each.failed }}
    next:
      - when: vars.failed > 0
        to: report
      - to: end

  report:
    type: notify
    command: 'notify-send "Arcflow triage" "$ARCFLOW_MESSAGE"'
    message: "${{ vars.failed }} of ${{ vars.triaged + vars.failed }} issues failed triage."

outputs:
  triaged: ${{ vars.triaged }}
  failed: ${{ vars.failed }}
```

`triage-one.yaml` (the subflow body):

```yaml
arcflow: 1
name: triage-one
include: [shared/templates.yaml]
inputs:
  issue: { type: object, required: true }
nodes:
  classify:
    extends: triager
    prompt: |
      Classify this issue and draft a first response.
      ${{ inputs.issue }}
    output_schema: schemas/triage.json    # {label, reply, confidence}
    next:
      - when: nodes.classify.output.confidence >= 0.7
        to: apply
      - to: end
  apply:
    type: shell
    env:
      ISSUE: ${{ inputs.issue.number }}
      LABEL: ${{ nodes.classify.output.label }}
      REPLY: ${{ nodes.classify.output.reply }}
    run: |
      gh issue edit "$ISSUE" --add-label "$LABEL"
      gh issue comment "$ISSUE" --body "$REPLY"
outputs:
  label: ${{ nodes.classify.output.label }}
```

---

## Appendix B. Event reference

All events carry `v`, `seq`, `ts`, `type`, and where relevant `branch`, `node`, `visit`, `attempt`. `data` holds the fields listed.

| Type | Data |
|---|---|
| `run_created` | `flow`, `flow_sha256`, `inputs`, `workdir`, `arcflow_version`, `parent` |
| `run_started` | `pid`, `host`, `on_wait` |
| `runner_attached` / `runner_detached` / `runner_takeover` | `pid`, `host`, `reason` |
| `flow_reloaded` | `old_sha256`, `new_sha256`, `snapshot` |
| `visit_started` | `type`, `config_ref` (path to `visit.json`), `config_sha256`, `wake_at` (sleep), `workspace` |
| `workspace_created` | `name`, `path`, `branch`, `base_commit`, `keep`, and `recreated: true` when `--recreate-workspaces` put it back |
| `workspace_removed` | `name`, `path` |
| `attempt_started` | `attempt`, `adapter`, `session_mode`, `resume_session_id` |
| `session_started` | `session_id` (fsynced immediately, §8.1) |
| `progress` | `kind` (`text`, `tool_call`, `tool_result`, `usage`, `log`), `summary` (sampled, at most 5 per second; full detail is in `stream.jsonl`) |
| `permission_denied` | `tool`, `reason` |
| `attempt_finished` | normalized result without large fields: `outcome`, `error`, `usage`, `cost_usd`, `stopped_by`, `session_id`, `session_total_usd` (agents) |
| `schema_retry` | `errors` |
| `visit_finished` | `outcome`, `result` (state fields, large values by reference) |
| `route_taken` | `from`, `to`, `via` (`next` / `on_error` / `resume`), `case_index`, `reason` |
| `budget_updated` | `usd_spent`, `tokens_spent`, `usd_left`, `tokens_left` |
| `human_waiting` | `message`, `choices`, `input`, `ack`, `show`, `default`, `deadline`, and `kind: "resume"` for an `on_resume: ask` prompt |
| `hook_ran` | `hook` (`on_wait`), `exit_code`, `duration_s` |
| `human_responded` | `choice`, `text`, `acknowledged`, `responder`, `via` |
| `cancel_requested` | `by`, `reason` |
| `child_run` | `run_id`, `item_index` (map) |
| `warning` | `code`, `message` (e.g. `W-TORN-LOG` when a torn last line was dropped) |
| `run_waiting` | `nodes` (pending) |
| `run_reopened` | `previous` (the status `--force` reopened) |
| `run_succeeded` | `outputs`, `totals` |
| `run_failed` | `reason` (`node_error`, `route_fail`, `no_route`, `max_visits_exceeded`, `max_steps_exceeded`, `max_duration_exceeded`, `budget_exceeded`, `expression_error`, `engine_error`), `node`, `message`, `totals` |
| `run_cancelled` | `by`, `reason`, `totals` |

---

## Appendix C. Validation codes

Severity prefix: `E` error, `W` warning, `I` info. The list is stable; codes are never reused.

| Code | Meaning |
|---|---|
| `E-YAML` | YAML syntax error |
| `E-YAML-ALIAS` | Anchors, aliases or merge keys used |
| `E-DUPLICATE-KEY` | Duplicate mapping key, or a template defined both locally and in an `include` |
| `E-SCHEMA` | Type or structure does not match the flow schema |
| `E-UNKNOWN-KEY` | Key not allowed here |
| `E-VERSION` | `arcflow:` version not supported |
| `E-BAD-ID` / `E-RESERVED-ID` | Identifier malformed / reserved |
| `E-UNKNOWN-TEMPLATE` / `E-TEMPLATE-CYCLE` / `E-TEMPLATE-ROUTING` | `extends` problems; routing keys in a template |
| `E-UNKNOWN-TARGET` / `E-UNKNOWN-START` | Edge or start target does not exist |
| `E-DEFAULT-NOT-LAST` / `E-MULTIPLE-DEFAULTS` | Case list order |
| `E-CONDITION-NEEDS-DEFAULT` | `condition` node without a default case |
| `E-NO-EXIT` | A node from which neither `end` nor `fail` is reachable |
| `E-FILE-NOT-FOUND` | Referenced file missing |
| `E-BAD-JSON-SCHEMA` | Output or input schema is not a valid JSON Schema |
| `E-EXPR-SYNTAX` / `E-EXPR-FORBIDDEN` / `E-EXPR-UNKNOWN-FUNCTION` | Expression problems |
| `E-UNKNOWN-REF` | Reference to an unknown input, node, field or schema path |
| `E-UNKNOWN-HARNESS` / `E-HARNESS-OPTIONS` | Adapter not installed / options invalid |
| `E-PERMISSION-UNSUPPORTED` | Adapter lacks the permission profile |
| `E-SESSION-HARNESS` | `resume`/`fork` across different harnesses |
| `E-MUTUALLY-EXCLUSIVE` | e.g. `prompt` and `prompt_file`, `run` and `args`, `duration` and `until` |
| `E-WORKSPACE-NO-GIT` | Worktree requested outside a git repository |
| `E-NOT-IMPLEMENTED` | Feature specified but not available in this version |
| `E-EXPR-RUNTIME` / `E-NO-ROUTE` / `E-WORKSPACE-MISSING` | Run-time counterparts, reported in events |
| `W-NO-DEFAULT-ROUTE` | Case list without a default |
| `W-UNREACHABLE` | Node not reachable from `start` |
| `W-UNGUARDED-CYCLE` | Cycle with no `visits` guard |
| `W-ERROR-NEVER-ROUTED` | `next` inspects own failure but `on_error: fail` |
| `W-NEVER-SET` | Reference to a node that cannot have run yet |
| `W-UNBOUNDED` | A limit set to `none` |
| `W-USD-UNENFORCEABLE` | USD budget on an adapter without cost and no price |
| `W-IGNORED-OPTION` | Option the adapter does not support |
| `W-CODEX-STRICT-SCHEMA` | Schema likely rejected by Codex strict mode |
| `W-JINJA-LIKE` | `{{ }}` without `$` |
| `W-SHELL-INTERPOLATION` | Unquoted node/var data interpolated into `run` |
| `W-FULL-PERMISSIONS` | `permissions: full` |
| `W-NO-HUMAN-BEFORE-RISKY` | Risky command reachable without a human node |
| `I-UNCHECKED-OUTPUT` | `output` fields of a node without a schema are not checked |
| `I-EXTRA-ARGS` | `harness_options.extra_args` used |
