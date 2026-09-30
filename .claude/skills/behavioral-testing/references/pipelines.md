# Pipeline behaviors

Pipeline behaviors (the `behaviors_<flow>_pipeline` convention) answer
**"do the subsystems compose correctly?"** — drive the whole application
end-to-end (input → internal dispatch → state update → output assembly → the
observable output surface) and assert on what a consumer would actually
*observe*, via that surface's semantic queries, not on internal state.

The **observable output surface** is whatever the outside world sees, named in
your project's terms:

- A web service: the HTTP response — status, headers, body — for a request
  driven through the real router.
- A CLI: stdout / stderr / exit code for a command driven through the real
  arg parser.
- A library: the return value / emitted events for a call sequence through the
  public API.
- A terminal UI: the rendered viewport — what text is visible, what a given row
  shows — after events flow through to render.

Use these when the bug class you're catching lives in the seams between correct
components — each subsystem works in isolation but nothing tests them wired
through to the produced output. The canonical shape is three correct components
(say a request router, a state store, and a response serializer, or a scroll
model, a viewport, and a renderer) with no test driving one input all the way to
the output the consumer sees.

**Any input-handling subsystem must have at least one pipeline scenario.**

Archetypes, one per domain: a request/response pipeline (route → handle →
serialize), a command pipeline (parse args → execute → print), an edit pipeline
(type / delete / newline / move → render), a scroll/navigation pipeline (wheel,
page, arrow-follow → viewport).

## When to escalate to a real-boundary smoke

For bugs that depend on the actual outer surface — the real network socket, the
real OS clipboard, the real terminal, the real browser DOM — injecting events
*past* that surface is not enough; it bypasses the boundary where the bug lives.
Drive the real boundary instead:

- A web service: a real HTTP client over a real socket, exercising real
  content-negotiation, chunked transfer, or connection-close semantics — not a
  handler called with a synthetic request object.
- A terminal UI: a real pseudo-terminal carrying escape sequences, bracketed
  paste, focus events, and protocol keys — not events injected past the parser.
- A browser app: a real DOM in a real headless browser, not a virtual-DOM
  snapshot.

A bypass test passing is not a signal that the fix works here — **"all tests
pass" only means something if the test exercised the surface where the bug
lives.** Test the real boundary where the bug actually lives, not a convenient
bypass of it.

## Red flags

- A pipeline test that bypasses the produced output and inspects internal state
  directly. → That's a behavior test wearing pipeline clothes. Drive the whole
  flow and assert on what's produced (the response, the stdout, the return
  value, the rendered output).
- A behaviors file that boots the whole application for every test. → Either
  it's actually a pipeline file (rename) or the subsystem can be exercised
  directly (downgrade to a behavior).
- A pipeline scenario that calls into internal items via a testing-only path
  the public contract doesn't expose. → In an out-of-tree / black-box ecosystem,
  either the path is contract (promote) or the test is pinning structure
  (delete). Where your ecosystem blesses white-box access (Go same-package,
  Rust `#[cfg(test)]`, JS co-located), reaching an internal directly is
  legitimate — that's the local convention, not a violation.
