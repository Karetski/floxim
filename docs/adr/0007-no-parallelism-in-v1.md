# 0007. No parallelism in v1

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §6 row 7

## Decision

v1 runs one node at a time per run. The event log and state model are designed so `parallel`/`join` and `map` fan-out can be added later (node visits keyed by branch).

## Consequences

No worktree merge policy, concurrent log interleaving or concurrent budget accounting in v1. `map` in ADR 0002 runs items sequentially until parallelism is added.
