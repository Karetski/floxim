# 0002. Flow file format and expressions

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §3, §6 row 2, §8.2

## Context

Alexey requires a single source of truth for each flow that the TUI, an agent, and the user in their own editor can all read and write, with changes visible live in each (2026-09-29). A flow written as code can't be safely written back by a TUI, and previewing it means executing it.

## Decision

- Each flow is **one YAML file** (parsed as YAML 1.2, strict: unknown keys rejected) and is the single source of truth. Nothing generates it; there is no Python builder API or other second representation.
- A **published JSON Schema** describes the file, for editor completion and for agents authoring flows.
- Edits by Floxim (TUI, CLI, agents working through Floxim) use `ruamel.yaml` round-trip mode, so comments, key order and formatting survive, and are written atomically. If the file changed on disk since it was read, Floxim reloads and asks before overwriting.
- Composition lives in the format: `subflow` (call another flow file), `map` (run nodes per list item), reusable node `templates` with per-node overrides, and `include` of shared fragments.
- Custom logic lives beside the graph: a `python` node references a function (`call: pkg.module:fn`) in an ordinary module.
- **Expressions** use a restricted Python-syntax subset, evaluated without access to builtins, imports or dunder attributes: bare in `when:`, and interpolated as `${{ expr }}` in strings. Long prompts may live in separate files (`prompt_file:`).
- `floxim validate` checks that every `nodes.<id>.<field>` reference exists in the referenced node's declared outputs or output schema.

## Consequences

- The TUI graph view, agent authoring (`floxim new`), and manual edits all converge on one file; git diffs are the change history.
- No CEL type-checking; validate-time reference checks replace most of it.
- Loops over data or generated graphs must be expressed with `map`/`subflow`/`templates`, not arbitrary code.

## Alternatives considered

- Python flow code (lionagi-style): rejected, not round-trippable.
- YAML plus a Python builder: rejected by Alexey as a second source.
- CEL expressions: its only Python implementation needs native code (see ADR 0001).
- TOML, CUE, JSON: less readable for graphs and prompts.

## Amendment (2026-09-29)

`include` brings in **templates only** (spec §3.7, §15 Q3). Nodes, inputs and routing always live in the flow's own file, so the graph can be read and edited from one file. Decided by Alexey in the spec review.
