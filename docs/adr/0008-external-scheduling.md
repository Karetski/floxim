# 0008. Scheduling is external

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §6 row 8

## Decision

Loom has no scheduler or daemon in v1. Scheduling uses cron, systemd timers or CI. Loom supports this with stable, documented exit codes, `--json` output on every command, and documentation recipes for each scheduler.

## Consequences

Matches brief principle 7. A `loom schedule` helper that generates crontab or systemd units may come later.
