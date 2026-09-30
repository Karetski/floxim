# MLIR — extend one primitive, don't add new ones

> **Principle.** Every IR concept is an `Operation`; new domains are dialects over the same primitive instead of parallel concept hierarchies.

**Forbids.** Sibling subsystems for things that should be variants of an existing primitive. A new kind of thing — a new resource type in an API, a new job kind in a scheduler, a new message type on a bus, a new surface (settings page, secondary view, embedded terminal) in a UI — is a variant in the existing structural vocabulary, never a new top-level concept with its own lifecycle.

## Question it answers

*What is this thing?*

## What "right" looks like

The system's structural vocabulary is one closed set of kinds — IR operations for a compiler, resource types for an API, node kinds for a document tree, job kinds for a scheduler. Anything the system operates on is a variant of that vocabulary, not a parallel hierarchy with its own dispatch, storage, or lifecycle. New capability rides inside the existing primitive (a new dialect over `Operation`, a new variant of the resource enum, a content slot on an existing node) rather than introducing a new top-level type.

The gain is that every generic operation over the structure — traversal, serialization, validation, dispatch — works on the new thing for free, because it's the same primitive.

## Red flags

- Proposal phrased "we'll add a new kind of …" → Find the existing primitive it should specialize.
- A subsystem with its own dispatch, storage, and lifecycle running parallel to the main structure (a second renderer beside the UI tree, a second request pipeline beside the API's, a second job runner beside the scheduler's). → Push it into the existing structural vocabulary as a variant.
- A top-level type whose lifecycle is parallel to the main structure rather than nested inside it.
- A new variant enum or registry added next to the existing one, "to keep things separate." → The whole point is that there isn't a separate.

## Note on scope

This is a design-review canon, not a code map. The red flags above are illustrative — the sharpest, most quotable form of the violation is stated in *your* project's own structural nouns (whatever your node kinds and content slots are actually called). Translate the flags into that vocabulary when you apply them; the principle is what's durable.

## Source

Lattner et al., *MLIR: A Compiler Infrastructure for the End of Moore's Law*, arXiv:2002.11054.
