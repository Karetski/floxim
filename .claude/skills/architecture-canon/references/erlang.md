# Erlang/OTP — supervised isolation, let it crash

> **Principle.** Build from many small isolated components that fail independently, communicate by messages, and are organized into supervision trees that restart failed children.

**Forbids.** A wedged extension, a runaway background computation, or a crashed external service taking the whole application down. Each is a supervised component with a restart policy; faults propagate to a supervisor, not to a `try { ... }` two layers up.

## Question it answers

*What happens when it fails?*

## What "right" looks like

Long-running subsystems — background workers, extension runtimes, external service clients — are supervised components. Each has a restart policy (e.g., "N restarts in a sliding window, escalate on exhaustion"). When a component crashes, the supervisor restarts it; if restarts exhaust, the fault escalates as a message on the event bus so the rest of the system observes the failure as a message, not as a thrown exception or a frozen UI.

Failure is bounded by isolation: one extension going down is one component going down, not the whole application.

## Red flags

- A `try { ... } catch { swallow }` (or a discarded error result) two layers above where the fault originated. → That layer isn't the supervisor; route the fault up as an event and let the supervisor decide.
- A new long-running task spawned directly with no restart policy. → Wrap it in the supervise primitive.
- A subsystem whose failure mode is "the whole app freezes" or "the whole app exits." → That subsystem must be isolated behind a supervisor.
- An external service, extension, or parser running on the application's main thread. → Move it to a supervised component.

## Source

Joe Armstrong, *Making Reliable Distributed Systems in the Presence of Software Errors*, KTH, 2003.
