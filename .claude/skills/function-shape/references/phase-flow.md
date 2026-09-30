# Phase flow & cyclomatic ceiling

> Most functions should be small because they name *one concept*, not because someone is chasing a line count.

## Phase flow

A function reads top-to-bottom as:

1. **Guard** the impossible or irrelevant case early.
2. **Normalize** inputs into boring local variables.
3. **Build** one small piece of explicit state.
4. **Walk** the domain structure in source order.
5. **Delegate** policy branches to named helpers.
6. **Return** a plain value, propagate a typed error (or panic for a true invariant violation), or emit an event.

If you can't feel those phases when re-reading, the function is doing more than one thing. Split or rename.

## Cyclomatic ceiling

Ordinary application logic should have only a few real decisions.

- **~10 independent branches** — stop and look for named phases, data-driven decisions, or a boundary object.
- **~15** — the rough upper edge for code that still wants to be read as normal application logic.
- **Beyond ~15** — isolate the mess in a *containment zone*: parser, stream adapter, command dispatcher, render loop, layout walker. Containment zones are allowed to be tangled because the outside world arrives that way; the surrounding code is not.

## Red flags

- A function with 8+ branches and a vague name like `process`, `handle`, `update`. → The name is the smell. Find the one concept and rename, or split into named phases.
- A boolean flag parameter that flips behavior between two code paths inside one function. → Two functions. The flag was hiding the split.
- A branch over a discriminated union / tagged variant where each arm calls into unrelated subsystems. → The walk is fine; the unrelated dispatch belongs in a dispatcher type, not in the walker.
