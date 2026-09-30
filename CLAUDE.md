# Loom

Loom (working name) is a terminal-first, harness-agnostic workflow orchestrator for AI coding agents. Users define flows (graphs of `agent`, `shell`, `condition`, `human` and `sleep` nodes) in YAML files; Loom runs them durably by driving existing harnesses (Claude Code, Codex CLI) in headless mode. The CLI is the engine; a TUI is a client on top.

## Where things are

| Read | For |
|---|---|
| `PROJECT_BRIEF.md` | Entry point: goals, design principles, non-goals, project plan and milestones (§15), working agreements (§16). Read it before starting work. |
| `docs/spec.md` | The authoritative definition of behavior: flow format, expressions, node catalog, execution semantics, storage, adapters, CLI/TUI. |
| `docs/adr/` | Accepted decisions, indexed in `docs/adr/README.md`. |
| `docs/research.md` | Background and evidence behind the ADRs, including tested harness behavior. |

Precedence: ADRs and the spec override the brief. When they disagree, the more specific, later document wins.

## Rules for this project

- **The spec is the source of truth.** If it is ambiguous or wrong, stop and raise it; don't invent behavior.
- **Stay inside the current stage and milestone** (see the brief's status table and §15). Nothing from later milestones or the non-goals.
- **Python 3.10+, pure-Python runtime dependencies only** (ADR 0001). A dependency that ships compiled code is not acceptable.
- **Tests first.** Engine tests use the fake adapter; unit tests never call a real harness or spend money.
- **Record non-trivial decisions** as a new ADR in `docs/adr/NNNN-title.md` and add it to the ADR index.
- **Keep docs in sync.** A behavior change updates the spec (and README, once it exists) in the same change.
- **Verify external facts.** Harness CLIs and SDKs change often; check current docs instead of relying on memory.

## Coding conduct

@.claude/conduct.md

## Vendored agentkit

`.claude/conduct.md` and `.claude/skills/` are verbatim copies from [agentkit](https://github.com/Karetski/agentkit) at commit `e8216e1`. Edit them upstream in agentkit and re-copy; don't edit the copies here. `hot-path-budget-audit` is intentionally not vendored: Loom's engine is I/O-bound, with no per-tick latency budget. Ignore mentions of it in the conduct and other skills.

Re-sync from a local agentkit checkout:

```sh
AK=~/Developer/agentkit
for s in architecture-canon behavioral-testing commit-discipline debugging-methodology \
         function-shape principle-review simplify-ruthless wire-drift-check; do
  rm -rf ".claude/skills/$s" && cp -R "$AK/skills/$s" ".claude/skills/$s"
done
cp "$AK/CLAUDE.md" .claude/conduct.md
```

Then update the commit hash above.
