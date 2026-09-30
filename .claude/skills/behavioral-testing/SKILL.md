---
name: behavioral-testing
description: How tests are shaped — behavioral and scenario-named (given/when/then), one file per public subsystem, driven through the surface a consumer actually observes. Where tests live (co-located vs out-of-tree) and how much white-box access they take is a per-ecosystem convention, not a universal law — the skill names the tradeoff and defers to your language's idiom. Use when writing or moving a test, deciding what to verify for a new public API, choosing a test's altitude (behavior / pipeline / invariant), or when a test wants to reach an internal item. Load only the reference files the question needs.
---

# behavioral testing

Tests are **behavioral** first: each one names a scenario and asserts on what a
consumer of the code actually observes, not on incidental internal structure.

## The durable core (applies in any language)

1. **Scenario names, sectioned bodies.** `given_<setup>_when_<action>_then_<outcome>`
   (or `when_…_then_…` when there's no precondition). Bodies sectioned with
   `// Given` / `// When` / `// Then` headers. Reading the name tells you the
   behavior being claimed.
2. **Assert on observable behavior, not incidental structure.** Test what the
   code *does* through a stable surface, so the test survives any refactor that
   preserves behavior. A test pinning a private helper's shape breaks on every
   internal change and verifies nothing a caller cares about.
3. **One `behaviors_<area>` file per public subsystem**, not one per source
   file — grouped by the subsystem under test.

Plain built-in assertions. No snapshot, property, parameterized, or
pretty-diff libraries by default — those are separate decisions, not defaults.

## Placement & visibility — a convention, not a law

*Where* tests live and *how much* white-box access they take is an
ecosystem-specific choice with a real tradeoff, not a universal rule. Pick the
convention your language already blesses and hold to it consistently:

- **Out-of-tree, black-box** — a dedicated tests directory, public contract
  only. Maximizes refactor-resilience and keeps tests honest about the public
  surface. Idiomatic for integration / end-to-end tests everywhere, and the
  default for *unit* tests in some ecosystems.
- **Co-located and/or white-box** — tests beside the source, free to reach
  internal items. Idiomatic and *recommended* in others: Rust `#[cfg(test)]`
  modules, Go same-package `_test.go`, JS/TS `.test.ts` beside source, Swift
  `@testable import`. Testing an internal directly is normal there.

The durable rule is **black-box first**: prefer to drive behavior through the
public surface, and reach for an internal only when the alternative is a
strictly weaker test. When a test *does* need an internal item, the moves are:
drive it through the public entry point that already uses it, promote the item
to public (it was really contract), or reach it via your ecosystem's white-box
mechanism if that is the local convention. Treat "migrate a co-located block
out of the tree" as a fix only when your ecosystem's convention is out-of-tree
to begin with — in Rust/Go/JS it usually isn't.

## Layout

A naming scheme for the test files, wherever your ecosystem puts them (a
dedicated tests directory, or beside the source):

```
behaviors_<area>              ← scenarios for one public subsystem
behaviors_<flow>_pipeline     ← end-to-end input→state→output flows
invariants_<name>             ← perf / alloc / depth pins (see below)
common/ (shared fixtures)     ← a shared-fixtures module
<benches-dir>/                ← bench-harness hot-path benches
```

One `behaviors_*` file per public subsystem, not one per source file.

**Language-adapter note.** `behaviors_<area>` names the *grouping* — one file
per public subsystem — not a literal filename. The physical name follows your
ecosystem's convention: `orders.test.ts` / `orders.spec.ts` (JS/TS),
`test_orders.py` (Python), `orders_test.go` (Go), a `behaviors_orders` binary
(a compiled out-of-tree language). Don't rename an idiomatic `orders.test.ts` to
`behaviors_orders`; map the grouping concept onto the local name. In ecosystems
where each top-level test file is its own compiled test binary, fewer files also
keep link/build time bounded — one more reason to group by subsystem. The
shared-fixtures module is imported by each behavior file. The grouping rule (by
subsystem) is durable; the physical name and location are your toolchain's
convention (see *Placement & visibility* above).

## Topic → reference

Pick the row that matches the work in front of you. Don't pre-load all references.

| Question | Reference |
|---|---|
| How do I shape a single behavioral test? Body shape, table-driven cases, red flags. | `references/behaviors.md` |
| When is a pipeline test the right tier? Which seams does it catch? | `references/pipelines.md` |
| When is something a physical invariant, not a behavior? Allocation counters, depth pins. | `references/invariants.md` |
| Where do benches live? Two tiers, harness setup, CI smoke. | `references/benches.md` |
| My ecosystem is out-of-tree and I'm moving a co-located block. How? | `references/migration.md` |

The **invariant** tier (allocation counters, depth pins) is most relevant to
systems / performance-critical code; on a typical app most tests are behaviors
and pipelines. For observability / span placement and its interaction with
allocation invariants, see `references/invariants.md` (the "instrumentation"
section).

## Scope

This skill applies to tests in any project. The durable spine — behavioral,
scenario-named, black-box-first, grouped by subsystem — is language- and
tool-neutral. The mechanics that vary by ecosystem — where tests physically
sit, whether unit tests are co-located, how test binaries are compiled, how
internal visibility works — are the language-adapter layer: adopt your
toolchain's blessed convention (see *Placement & visibility*), keep the spine
intact.

## See also

Stands alone — the skills below are optional companions, not dependencies.
`hot-path-budget-audit` turns its verdicts into durable proof via the invariant
tier (`references/invariants.md`); `wire-drift-check` relies on the roundtrip
coverage the behavior and pipeline tiers provide. Companion standards:
`architecture-canon`, `function-shape`, and the always-on coding conduct.
