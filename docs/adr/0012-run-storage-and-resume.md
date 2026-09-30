# 0012. Run storage and resume semantics

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §2.4, §2.5, §6 additional proposals

## Decision

- Each run lives in `.loom/runs/<run-id>/`: `events.jsonl` (append-only, the source of truth), `state.json` (derived snapshot, rebuildable from events), and `nodes/<id>/<visit>/` for prompts, raw harness streams, stdout/stderr and artifacts.
- State is checkpointed after every node transition.
- Node visits are **at-least-once**. A completed visit is never rerun on resume. An in-flight agent visit resumes its harness session by default when a session ID was recorded; `on_resume: restart` reruns it instead.
- Human nodes are their own node type, so nothing reruns around a pending answer; `loom respond` appends an event.

## Consequences

Resume after a crash is deterministic from the event log. Shell nodes with side effects should be idempotent or marked `on_resume: restart` with care; the spec defines the exact rules.
