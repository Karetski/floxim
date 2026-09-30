# 0006. Human node delivery

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §6 row 6

## Decision

In v1, pending human nodes are answered through the CLI (`floxim respond`) and the TUI. A flow or node may set an `on_wait` shell command hook that Floxim runs when a human node starts waiting, for notifications (desktop, Slack, email) without built-in integrations.

## Consequences

Small core. A built-in `notify` node may come later.
