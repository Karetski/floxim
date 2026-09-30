---
name: architecture-canon
description: Ten borrowed architectural north stars (Hickey, Plan 9, Acton, Erlang, MLIR, LSP, VS Code, Smalltalk, SICP, Observability), each answering one design question with an explicit forbids clause and quotable red flags. Use when designing, proposing, auditing, or reviewing anything that adds public surface — a new type, module, interface, event kind, config key, or extension point — when asking "should we add X" or "how should we structure Y", or when a bug's proper fix introduces such surface. The principles are hard constraints, not stylistic preferences. Load only the reference files the current decision needs.
---

# architecture canon

Ten borrowed ideas, each answering one specific design question. This skill is the operational form: each star lives in `references/<star>.md` with its forbids clause and concrete red flags reviewers can quote. It speaks only to architecture — the shape of subsystems and the surfaces between them.

## Question → star lookup

When a design choice is up in the air, find the question, load the reference, walk the forbids. The **Applies to** column tells you whether a star bites on any codebase or only in its native habitat — don't force a habitat-bound star onto code with no such surface (a React app has no extension protocol; a CRUD service has no per-frame budget).

| Question | Star | Applies to | Reference |
|---|---|---|---|
| Is it doing one thing? | Hickey | **any codebase** | `references/hickey.md` |
| Does it have primitives, combination, and abstraction? | SICP | **any codebase** | `references/sicp.md` |
| What is this thing? | MLIR | any codebase *with a structural vocabulary / "kind" enum*; where none exists it reduces to Hickey's one-thing check | `references/mlir.md` |
| How does an extension reach it? | LSP | plugin / integration surfaces; the M→M+N insight generalizes | `references/lsp.md` |
| What does the extension contribute? | VS Code | projects with a real plugin/extension surface | `references/vscode.md` |
| How is it addressed? | Plan 9 | systems with many resource kinds (OS, storage, routing) | `references/plan9.md` |
| How does it lay out in memory? | Acton | systems / games / hot-path perf code | `references/acton.md` |
| What happens when it fails? | Erlang | concurrent / fault-tolerant / long-running services | `references/erlang.md` |
| How is it bound? | Smalltalk | codebases with an event/message spine, or a feature that is inherently event-shaped | `references/smalltalk.md` |
| How is it observed? | Observability | services / distributed / production ops | `references/observability.md` |

Rule of thumb: **Hickey and SICP apply to any design; MLIR too wherever there's a structural "kind" vocabulary** (and where there isn't, it degrades to Hickey's one-thing check rather than forcing a finding). The rest earn their keep only when the code actually has the surface they describe. If you're reviewing a design and a star's habitat isn't present, that star simply doesn't apply — say so and move on; don't manufacture a violation.

## How to apply

Three modes:

**Proposing a feature.** Walk the rows whose habitat the proposal actually has (always the universal three; the rest only if the surface is present); load each reference and name the star's answer. A row that applies but has *no* answer means the proposal isn't ready — say so, name the missing piece, don't paper over it. A row whose habitat is absent is skipped, not failed.

**Auditing existing code.** Each reference has a *red flags* section. If the code matches a red flag, the violation is real — name the star, quote the forbids clause.

**Reviewing a PR.** Both: place the change against the relevant rows, then scan the touched files against the red flags for those stars. An extension-surface review loads four references (MLIR + Plan 9 + LSP + VS Code); a hot-path data refactor loads two (Acton + Hickey); a span-placement audit loads two (Observability + Erlang). Pick from the table, not all ten.

Don't pre-load all ten references. Pick from the lookup table; load only what the work needs.

## Finding the canonical pattern

References are deliberately abstract — they don't pin to specific files or symbols, because anchors rot when code moves and stale anchors teach the wrong lesson. When you need a concrete example of how a star is realized in *this* codebase, search for the relevant concept directly and read the live code. The principle is the durable artifact; the implementation under it can change without the skill changing.

## Tone

The principles are direct. Apply them directly. "This violates Plan 9 — two resource kinds share one namespace already, and you're proposing a second registry with its own lookup" is the right phrasing. Hedged "maybe consider possibly using…" is not. The principles are the bar; either a proposal clears it or it doesn't. Name the violation, quote the forbids, and point at the relevant existing concept when describing what right looks like.

When two stars conflict on a design (rare, but it happens — e.g., a late-bound message bus that obscures memory layout), name both and the tradeoff explicitly. Don't pretend it's clean. The user decides which star wins; record the reasoning.

Conversely, when several stars flag the *same* root cause — a feature built as a standalone silo will often trip MLIR (parallel subsystem), Plan 9 (second addressing scheme), Smalltalk (direct wire), and Hickey (braided type) at once — report it as **one** defect with several lenses as independent reasons, not as four separate findings. The lenses corroborate; they don't multiply the problem count.

## See also

Stands alone — the skills below are optional companions, not dependencies. These apply the same stars to a finished change: `principle-review` (whole-change review), `hot-path-budget-audit` (enforces the Acton and Observability stars on a latency-critical path), and `wire-drift-check` (enforces the Plan 9 single-source-of-truth across a duplicated boundary). Companion standards: `function-shape`, `behavioral-testing`, and the always-on coding conduct.
