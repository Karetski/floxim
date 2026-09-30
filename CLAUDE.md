# Floxim

Floxim is a terminal-first, harness-agnostic workflow orchestrator for AI coding agents. Users define flows (graphs of nodes such as `agent`, `shell`, `condition` and `human`) in YAML files; Floxim runs them durably by driving existing harnesses (Claude Code, Codex CLI) in headless mode. The CLI is the engine; a TUI is a client on top.

## Where things are

| Read | For |
|---|---|
| `README.md` | What the tool does today: flow format, node types, CLI, TUI, adapters, configuration. |
| `src/floxim/` | The implementation. |
| `tests/` | Behavior as tested; `tests/flows/` is the validation corpus. |
| `examples/` | Example flows, kept identical to their copies in the valid corpus. |

## Rules for this project

- **The code and its tests define behavior; the README describes it.** If they disagree, stop and raise it; don't invent behavior.
- **Python 3.10+, pure-Python runtime dependencies only.** A dependency that ships compiled code is not acceptable; CI checks it.
- **Tests first.** Engine tests use the fake adapter; unit tests never call a real harness or spend money.
- **Keep the README in sync.** A user-visible behavior change updates the README in the same change.
- **Verify external facts.** Harness CLIs and SDKs change often; check current docs instead of relying on memory.

## Coding conduct

@.claude/conduct.md

## Vendored agentkit

`.claude/conduct.md` and `.claude/skills/` are verbatim copies from [agentkit](https://github.com/Karetski/agentkit) at commit `e8216e1`. Edit them upstream in agentkit and re-copy; don't edit the copies here. `hot-path-budget-audit` is intentionally not vendored: Floxim's engine is I/O-bound, with no per-tick latency budget. Ignore mentions of it in the conduct and other skills.

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
