# 0014. v1 scope

- Status: Accepted
- Date: 2026-09-29
- Decided by: Alexey (spec review, spec §15 Q4)
- Context: PROJECT_BRIEF.md §15, spec tiers and §15, research.md §8

## Context

The brief's milestones (M0–M8) cover the five basic node types, the built-in adapters, the CLI and a TUI for monitoring and control. The spec specifies more than that, because research (§8) raised goals the brief predates: flows edited from the TUI as well as by hand, composition inside the format, and adapters users write themselves. Draft 1 marked those features "Planned" and left their placement to Stage 3.

## Decision

v1 (the first release) contains the spec's Core tier plus:

- **Structured editing:** the `arcflow.edit` library, the `arcflow flow <op>` commands, and the TUI graph editor with live two-way sync (spec §10.3–10.4).
- **The remaining node types:** `set`, `python`, `subflow`, `map`, `handoff`, `notify` (spec §5.6–5.11), and `include` of templates (spec §3.7).
- **User-written adapters without Python:** command adapters (spec §8.5), and the conformance kit `arcflow adapter test` (spec §8.8).

These features are marked Core in the spec. The following stay **Planned** (after v1): agent-authored flows (`arcflow new`, `arcflow edit`, the TUI's "Edit with agent"; spec §11), the Arcflow Adapter Protocol (spec §8.6), and the ACP adapter (spec §8.7).

## Consequences

- v1 is roughly twice the size of the brief's M0–M8. `docs/milestones.md` keeps the brief's milestone numbers and meanings, splits milestones too large for one agent session into lettered parts, and slots the added features in as lettered milestones where their dependencies place them.
- The flow format is complete at v1: every node type in the catalog runs, so later releases add behaviour, not syntax.
- Agent authoring can be built on structured editing and the conformance-tested adapters once v1 has shipped.

## Alternatives considered

- **Brief's M0–M8 only:** the smallest release, but it ships a format in which several documented node types are rejected with `E-NOT-IMPLEMENTED`.
- **Everything including agent authoring:** the largest release; agent authoring depends on structured editing and stable adapters, so it gains most from coming after them.
