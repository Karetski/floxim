---
name: function-shape
description: How an individual function or helper should be shaped — phase flow, a cyclomatic branch ceiling, error-handling moves, pipeline naming, mutation ownership, and when a comment earns its line. Use when writing or reviewing a single function, helper, or tiny owner type; when a function exceeds ~10 real branches and the question is extract-or-contain; when choosing between panic / absent value / typed error / encoded protocol failure; when naming a multi-step pipeline; or when judging whether a comment or helper earns its existence. Load only the reference files the question needs.
---

# function shape

This skill speaks only to what a single function looks like — its internal shape, not the architecture around it or the agent's conduct while writing it.

## Question → reference

Pick the row that matches the decision in front of you. Don't pre-load all references.

| Question | Reference |
|---|---|
| Does this function read as one concept? Is it past the branch ceiling? | `references/phase-flow.md` |
| Should this failure be a panic, an absent value (None/null), a typed error, or an encoded protocol failure? (the last assumes a supervised/actor lifecycle — most code uses the first three) | `references/error-handling.md` |
| What do I name the steps of a multi-step pipeline? | `references/pipeline-names.md` |
| Where should this mutation live? Does this owner earn its existence? | `references/mutation-owners.md` |
| Does this comment earn its line? Doc comment or inline? | `references/comments.md` |

## Style choices inside the line

These are short enough to live in the index, not a separate file.

- Prefer explicit loops when state evolves across records.
- Prefer chained transforms (map / filter / flat-map / collect) for pure projections.
- Use closures only when they capture a tiny invariant better than a detached helper would.
- Read top-to-bottom without a mental trampoline park.

## Scope

This skill applies to a single function, helper, or tiny owner type in any project and any language. The examples are language-neutral; where an idiom is shown, it is illustrative, not a mandate to use that language. Generated code, parsers, layout walkers, and other containment zones are exempt from the cyclomatic ceiling — but only those.

When a review finds a helper or owner type that doesn't earn its existence (`references/mutation-owners.md`), the fix is a deletion, not a rename.

## See also

Stands alone — the skills below are optional companions, not dependencies: `architecture-canon` (subsystem-level design), the always-on coding conduct, and `simplify-ruthless` (a ranked deletion audit for helpers that don't earn their keep).
