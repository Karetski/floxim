# Observability — structured spans, hot-path silence

> **Principle.** Runtime telemetry flows through one structured tracing pipeline so production code, tests, and diagnostics share one event stream.

**House rule, not canon.** The "levels budgeted by call frequency" convention below (INFO on cold paths, TRACE off by default on hot paths) is an authoring opinion, not part of the observability canon — standard practice controls volume with *sampling*, not level-by-frequency. It's a useful discipline where a hot path has a zero-allocation invariant to protect; treat it as a local convention, not a sourced law. The sourced principle is the single structured pipeline (one collector, structured events over ad-hoc prints).

**Forbids.** Ad-hoc print-to-stdout/stderr instrumentation in application code — it arrives without a level, a timestamp, or a request/trace id, and in some hosts never arrives at all (a full-screen terminal surface swallows it; a captured or daemonized process discards it). Adding spans inside a hot path without re-running the matching allocation-invariant test to confirm zero net allocations — a failing invariant means the span placement is wrong, not the invariant.

## Question it answers

*How is it observed?*

## What "right" looks like

One telemetry pipeline, gated by the application's log-filter env var. Levels are budgeted by how often the code runs:

- **`INFO` and above** — cold paths: process start, module/plugin load, connection open, file open, focus change.
- **`DEBUG`** — medium per-request / per-frame seams: request handler, render, event dispatch.
- **`TRACE`** — per-inner-loop work (per-row render, per-record walk, per-segment scan), kept *off* by default so the zero-allocation invariants stay green.

Adding a span at any level means re-running the matching allocation-invariant test (a counting-allocator test over the hot path) to confirm zero net allocations introduced. The test is the source of truth; if it fails after a span lands, the span placement is wrong.

## Red flags

- A raw print-to-stdout/stderr or debug-dump call in application code. → Replace with a tracing event at the appropriate level. Application stdout/stderr may be swallowed by a full-screen surface or discarded by a daemonized process, and carries no level or trace id even when it survives.
- A new span inside an inner-loop hot path (a record walk, a text wrap, a bus dispatch, a render) without a corresponding allocation-invariant run cited in the change. → Re-run it. If it fails, move the span out of the hot path.
- A `TRACE` span left enabled by default in a hot path. → `TRACE` is off by default; if it's running per-frame it's the wrong level.
- A subsystem with its own bespoke log channel (a growing list of debug strings, a side-channel file). → One pipeline. Route through the tracing spine.

## Source

Structured-tracing library documentation and its env-filter syntax.
