# Acton — programs are data transforms

> **Principle.** The purpose of a program is to transform data; design the data layout first, then the transforms over it.

**Forbids.** A boxed-trait-object-of-everything where a typed-key arena, a contiguous span, or a typed enum would do. Cache locality and batch shape are first-class design choices in hot paths (rendering, parsing, search), not optimizations bolted on later.

## Question it answers

*How does it lay out in memory?*

## What "right" looks like

Hot-path structures are arena-backed (a typed-key handle map, or a contiguous span), with typed enum variants instead of boxed trait objects. Tree walks use handle keys, not heap-allocated child vectors, so traversal is alloc-free. The zero-allocation invariant on hot walks is pinned by a test that installs a counting global allocator and fails on any allocation.

Choosing the data layout is part of the design step, not a follow-up "optimization."

## Red flags

- A boxed trait object on a render-, parse-, or search-path. → A handle map of typed variants, or a contiguous span.
- A walk that allocates a growable list per call. → Index over a handle-key range; pin with an allocation-counting test.
- A hash map keyed by a dense integer id where iteration order matters and the keys are dense. → A typed-key arena or contiguous store.
- "We'll optimize the layout later." → Layout *is* the design; choose it before writing the transforms.
- A list of trait objects where a list of a known enum's variants would do. → The enum is cheaper to dispatch and cache-friendlier.

## Off-hot-path is fine

Boxed trait objects on cold paths (one-shot construction, error formatting, etc.) are fine. The forbids fires on hot paths; name the path and the cost when applying.

## Source

Mike Acton, *Data-Oriented Design and C++*, CppCon 2014.
