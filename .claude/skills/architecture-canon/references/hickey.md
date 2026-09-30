# Hickey — simple is not easy

> **Principle.** *Simple* (un-braided, one role, one concept) is an objective property; *easy* (familiar, near to hand) is about the observer. Trading simple for easy is the most common, costliest mistake.

**Forbids.** A type that braids state, time, identity, and behavior because it's faster to write that way. If a review reveals a complected type, splitting it is the work, not the cleanup.

## Question it answers

*Is it doing one thing?*

## What "right" looks like

Types and modules have one role each. State lives in data structures; behavior lives in functions over them; lifecycle and identity are separated from both. When a "manager" or "host" type appears, it owns one concern, not several wearing one coat. When an interface exists, it abstracts one capability, not "everything a component can do."

When review finds a complected type, the split is done in the same change — not filed as a follow-up. That's how the codebase stays simple over time: complectedness compounds when ignored, and the fix gets harder with each new feature added on top.

## Red flags

- A record whose fields hold state, identity, lifecycle, *and* methods that mutate, observe, *and* dispatch. → Split into pure data + traversal + dispatch.
- An interface that exists to bundle "things a component can do." → Each capability is its own concern; a declarative statement of which capabilities a node supports beats a god-interface.
- An "easy" change that adds a field to a complected type instead of splitting it. → That field is rent on every future change; do the split now.
- A type whose name is a noun-verb ("ThingManager", "FooHost") that's clearly two things wearing one coat. → Two types.

## "Splitting is the work"

This is the principle's hardest sell: when you find a complected type mid-task, the right move is to split it *now*, not file a TODO. The split is what the task actually needed. If reviewers let braided types accumulate "for later," there is no later.

## Source

Rich Hickey, *Simple Made Easy*, Strange Loop 2011.
