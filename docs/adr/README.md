# Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-implementation-language.md) | Implementation language: Python 3.10+, pure-Python dependencies | Accepted |
| [0002](0002-flow-file-format-and-expressions.md) | One YAML file per flow as the single source of truth; Python-syntax expressions | Accepted |
| [0003](0003-state-and-addressing.md) | State and addressing model | Accepted |
| [0004](0004-workspace-isolation.md) | Shared working dir by default, opt-in Arcflow-managed worktrees | Accepted |
| [0005](0005-permissions-model.md) | Permission profiles mapped per adapter; prompts auto-denied when unattended | Accepted |
| [0006](0006-human-node-delivery.md) | Human nodes via CLI and TUI, plus an `on_wait` hook | Accepted |
| [0007](0007-no-parallelism-in-v1.md) | No parallelism in v1 | Accepted |
| [0008](0008-external-scheduling.md) | Scheduling is external (cron, systemd, CI) | Accepted |
| [0009](0009-name-deferred.md) | Final name deferred to a separate task | Superseded by [0013](0013-name-arcflow.md) |
| [0010](0010-license.md) | MIT license | Accepted |
| [0011](0011-harness-adapters.md) | Harness adapter architecture (four tiers) | Accepted |
| [0012](0012-run-storage-and-resume.md) | Run storage and resume semantics | Accepted |
| [0013](0013-name-arcflow.md) | Project name: Arcflow, CLI `arcflow` with alias `arcf` | Accepted |

Background for all of these: [../research.md](../research.md).
