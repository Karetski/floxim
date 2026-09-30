# Benchmarks

Benches live in a dedicated bench directory and run under a bench harness (a
measurement framework that samples and reports timing, distinct from the normal
test runner). *Illustrative:* in some toolchains you register each bench target
and disable the default test harness on it so the timing framework's flags
don't collide with the unit-test runner.

## Two complementary tiers

- **Hot paths**: pure primitives already pinned by an invariant test. The
  invariant proves *no allocation*; the bench quantifies *how fast*. This
  pairing is the point — a bench with no matching invariant is measuring noise
  or hiding a missing invariant.
- **End-to-end**: the cost of one full unit of work — one request through the
  stack, one CLI invocation, one run-loop turn. Drive the whole application
  through that unit and measure the baseline (an idle turn / a no-op request)
  against the loaded paths (an input burst, a populated request, an event
  drain). The baseline is the fixed cost of one unit; the other rows isolate
  handlers and dispatch.

## Running

*Illustrative commands* — map to your toolchain:

- a quick smoke run of every bench (bounded sample size, ~a minute)
- a compile-only build of the benches (what CI runs)
- a single named bench target

CI compiles the benches on every change so a broken bench surfaces immediately,
but runs timing **non-blocking** against a saved baseline — runner noise can
easily clear a small percentage budget, so a timing delta is a signal to
investigate, not a gate.

## Red flags

- A bench measuring something with no matching invariant. → Either the
  invariant is missing (add it; the bench would otherwise hide a
  regression-by-allocation) or the bench is measuring noise.
- A bench that takes more than a few seconds per iteration. → Push setup out of
  the timed region (batched / with-setup iteration) so the timed region is just
  the work.
- Benches wired so the bench harness collides with the unit-test runner. →
  Isolate the bench target from the normal test harness so a plain test run
  doesn't fail confusingly.
