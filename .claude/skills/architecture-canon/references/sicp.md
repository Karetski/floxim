# SICP — primitives, combination, abstraction

> **Principle.** Any expressive system has primitive elements, means of combining them, and means of naming combinations so they become primitives at the next level.

**Forbids.** A new feature whose layer doesn't have all three. If something is added without primitives, without a combinator, or without a naming/abstraction step, it isn't done — it's a one-off.

## Question it answers

*Does the layer have primitives, combination, and abstraction?*

## What "right" looks like

A clean layer has all three:

- A **primitive** type — the smallest meaningful unit at this layer.
- A **combinator** — a way to compose primitives into larger structures of the same kind.
- An **abstraction** — a named operation on the combined form that becomes a primitive of the layer above.

A canonical example in editors is the text-edit layer: a single change is the primitive, a transaction (a sequence of changes applied atomically) is the combinator, and named operations on transactions (apply, undo, redo) become the primitives of the editor layer above.

## Red flags

- A new submodule adds a primitive (`Foo`) and an operation (`apply_foo`) but no way to *compose* `Foo`s. → Add the combinator now, or shape the work so all three land together.
- A combinator exists but there's no named abstraction over the combined form (callers re-build the combination at each use site). → Name it.
- An abstraction is added on top of nothing — a wrapper without a primitive or combinator underneath. → Identify the missing layer; don't paper over it with a facade.

## Source

Abelson & Sussman, *Structure and Interpretation of Computer Programs*, §1.1.
