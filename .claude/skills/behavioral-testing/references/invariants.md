# Physical invariants

Invariants answer **"does this code obey a physical property the design
depends on?"** — zero allocations on a hot walk, bounded recursion depth,
branch-free dispatch, log pruning, a recovery byte sequence. These are
non-functional properties: the user never sees them directly, but the design
collapses if they break.

**Scope.** This tier is most relevant to systems, games, and
performance-critical code, where a physical property (no allocation, bounded
depth) is load-bearing. On a typical application most tests are behaviors and
pipelines; reach for an invariant only when the design genuinely rests on a
property no behavioral assertion would catch.

The archetype (illustrative): a counting-allocator hook installed for the test,
with allocation deltas asserted to be zero across the hot operation. New
invariant tests follow that shape and are recorded alongside the design
principle they protect.

Kinds of invariant worth pinning:

- A hot data-structure walk performs zero allocations.
- A segmenter / parser allocates only its bounded working set.
- A reentrant dispatch's depth counter returns to zero after draining.
- A log or buffer prunes to its bound instead of growing unboundedly.
- A recovery/teardown path emits an exact byte or call sequence.

**Run the matching invariant after touching a hot path or adding an
instrumentation span.** A failing invariant means your change is wrong, not the
invariant.

## Instrumentation: how spans interact with invariants

The only testing-relevant rule: **adding an instrumentation span inside a
hot path can introduce an allocation, so re-run the matching zero-allocation
invariant after the span lands. A failing invariant means the span placement is
wrong, not the invariant.**

The structured-tracing model itself — one pipeline, and log levels budgeted by
call frequency (INFO on cold paths, DEBUG per-request, TRACE off by default on
inner loops) — is a design concern, not a testing one. It lives in one place:
the Observability star of the `architecture-canon` skill. Consult it there
rather than re-deriving it here; this file owns only the "re-run the invariant
after a span lands" consequence.

## Red flags

- **A behaviors file that imports allocation counters or installs a counting
  allocator.** That's an invariant, not a behavior. Move it.
- **An invariant test asserting on user-visible state (rendered text, visible
  numbers).** That's a behavior. Demote.
- **A new hot-path function (a walk, a segmenter, a dispatch) without an
  invariant pinning its physical property.** Add one before the regression
  lands.
- **Don't mix tiers in one file.** A behavior scenario that *also* checks an
  allocation count is two tests in one body — split.
