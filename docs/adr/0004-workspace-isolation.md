# 0004. Workspace isolation

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §6 row 4

## Decision

Agent and shell nodes share the run's working directory by default. A node may opt in to `workspace: worktree`, in which case **Floxim** creates and manages a git worktree for it (not the harnesses' own `--worktree` flags, so behaviour is the same across harnesses).

## Consequences

Sequential flows see each other's changes without extra steps. Worktree lifecycle (creation, reuse across visits, cleanup, merging back) is specified in the spec and becomes mandatory once parallelism lands.
