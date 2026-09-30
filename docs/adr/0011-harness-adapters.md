# 0011. Harness adapter architecture

- Status: Accepted
- Date: 2026-09-30
- Decided by: Alexey (accepted the research.md §6 proposals when closing Stage 1)
- Context: research.md §1.5, §6, §8.1

## Context

Alexey wants a harness-agnostic adapter layer that users can extend with their own harnesses (2026-09-29). Research (§1) showed harness exit codes, budgets and session handling can't be trusted as-is.

## Decision

- **Adapter contract:** `capabilities()` (structured output, cost, budget, resume, fork, interactive handoff, permission hook), `start`, a stream of normalized events including `session_started` as soon as a session ID is known, `result`, `cancel`, and optional `interactive(session_id)`. Results carry a normalized outcome: `succeeded`, `failed`, `cancelled`, `budget_exceeded`, `timed_out`, `schema_invalid`.
- **Four tiers:** (1) built-in `claude`, `codex`, `fake`; (2) command adapters declared in `.loom/harnesses/<name>.yaml` (command template plus stream field mappings); (3) plugin adapters as Python entry points (`loom.adapters`) or any executable speaking the Loom Adapter Protocol (JSONL over stdio); (4) a generic ACP adapter.
- **The engine owns** timeouts, cancellation (SIGINT, grace period, SIGTERM, SIGKILL on the process group), and budget accounting; harness caps are only a backstop. Budgets accept tokens, USD, or both.
- **Success is judged from the final result event** and schema validation, never the exit code alone.
- Adapters set session IDs where the harness allows (Claude `--session-id`), capture them immediately otherwise (Codex `thread.started`), and run harnesses with a controlled environment that never passes through a parent agent session's variables.
- Missing capabilities degrade gracefully: Loom extracts and validates JSON itself when there's no native structured output, restarts when there's no resume.
- A published conformance suite, driven by recorded harness streams, applies to every adapter tier.

## Consequences

New harnesses need no engine changes. Contract fixtures must be re-recorded when pinned harness versions change.
