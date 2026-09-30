# 0003. State and addressing model

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §3.3, §6 row 3

## Decision

Expressions and templates see this namespace:

- `inputs.<name>`: run inputs.
- `nodes.<id>`: the **latest visit** of a node: `output` (validated structured output), plus type-specific fields such as `text`, `exit_code`, `stdout`, `stderr`, `session_id`, `usage`, `cost_usd`, `status`.
- `nodes.<id>.visits[i]`: all visits, oldest first, so loops can compare attempts.
- `visits.<id>`: visit count.
- `vars.<name>`: variables set by a future `set` node.
- `run.id`, `run.flow`, `run.started_at`: run metadata.

## Consequences

The common loop case (`nodes.test.stderr` inside a fix loop) stays short. Outputs are typed JSON, not strings. Parallel branches (post-v1, ADR 0007) will need visits keyed by branch; the event log must allow for that from the start.
