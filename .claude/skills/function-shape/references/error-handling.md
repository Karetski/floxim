# Error-handling: three moves, pick one

The choice is not vibes. Each move encodes a different claim about whose problem the failure is.

| Move | When | Example |
|---|---|---|
| **Panic / abort** | The caller violated a core invariant. The system can't proceed coherently. | A handle used after the resource it referenced was released; double-initialization of a singleton service. |
| **Return an empty-absent value (null/None) or a typed error** | Absence or expected failure is normal. The edge decides how to present or recover. | "No active component for this slot"; a working directory that doesn't exist on disk; a lookup key not registered; a parser found no match. |
| **Encode failure in the protocol** | A surrounding supervised lifecycle must survive the failure. This move assumes that architecture — a long-running task under a supervisor that observes typed outcomes. When that is the shape, model the failure as a typed failure event the supervisor observes, rather than unwinding. | A typed failure event published by the supervisor instead of tearing down; a tool/RPC reply with a typed error variant; a stream that yields an error event and continues. |

The third move has a precondition: it only applies when there *is* a surrounding supervised lifecycle that must keep running after the failure. If there isn't one, don't invent a supervisor to justify it — use one of the first two moves.

## Red flags

- A function whose job is "detect the issue" also formats, logs, prompts, or repairs. → Split. Detector returns facts; presenter formats; recoverer repairs.
- An invariant violation is silently logged and the function returns a default (or an error result is discarded). → That's the swallow-discard anti-pattern: a fault disappears where no one can react to it. Route it up as a typed failure the caller (or, if there is one, the surrounding supervised lifecycle) can observe — in plain code that just means raise/return it so the caller can react, no supervisor required.
- A long-running task panics on an external/provider failure that the surrounding subsystem is supposed to survive. → Encode the failure as a typed event and let the supervisor's restart policy decide; don't tear down the lifecycle.
