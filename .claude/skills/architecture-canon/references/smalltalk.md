# Smalltalk — late binding and messaging

> **Principle.** The kernel of object-orientation is *messaging*: extreme late binding of all things — what a name refers to, when a call dispatches, where state lives.

**Forbids.** Hard-wiring a dispatch that could be an event. If component X reacts to change Y by a direct method call, that's a fixed wire; if it subscribes to a named event, the binding is late and an extension can intercept it later. Default to late.

## Question it answers

*How is it bound?*

## What "right" looks like

Cross-component events flow through one event bus — a single late-binding spine that producers publish to and subscribers register against. The event catalog is a closed enum; extensions subscribe via the same API the core uses, with no privileged path. Reentrancy depth is bounded so the bus can't recurse unbounded, and a property test pins that invariant.

The bus is the default; direct calls are the exception that requires justification.

## Red flags

- Component X holds a direct reference to component Y and calls `y.on_change()`. → Publish a "Y changed" event; let Y subscribe.
- An "internal" event channel parallel to the bus. → Add an event variant; one spine.
- A new public method whose only callers are sibling subsystems reacting to a state change. → That's an event, not a method.
- A "fast path" that bypasses the bus for performance. → Justify explicitly; the indirection cost is rarely the bottleneck.

## When to hardwire (rare exception)

Tight loops in hot paths where the dispatch overhead has been measured and no extension will ever want to intercept. Document the decision at the call site.

## Note on scope

This is a design-review canon, not a code map. The red flags above are illustrative — their teeth come from being restated in *your* project's own vocabulary for events, the bus, and the components that publish and subscribe. Translate them into those nouns when you apply them; the "default to late binding through one named spine" principle is what's durable.

## Source

Alan Kay, *The Early History of Smalltalk*, ACM HOPL-II, 1993.
