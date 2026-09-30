# 0005. Permissions model

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §1, §6 row 5

## Decision

- Each agent node declares a permission **profile**: `read-only`, `edit`, or `full`. Each adapter maps the profile to its harness (Claude Code: permission mode plus tool allow/deny rules; Codex: sandbox mode plus approval policy).
- Harness-specific settings can be passed through per node for cases the profiles don't cover.
- Unattended default: `edit`, with anything that would prompt **denied automatically** (Claude: `--permission-mode dontAsk --permission-prompts none`; Codex: `--sandbox workspace-write`, approval policy `never`, set explicitly rather than taken from user config).
- Adapters deny nested orchestration by default (e.g. Claude's `Workflow` tool).

## Consequences

One vocabulary across harnesses. Denials must be reported in the node result so flows can branch on them. Later: route permission prompts to a human node (Claude `--permission-prompt-tool`, Codex app-server approvals).
