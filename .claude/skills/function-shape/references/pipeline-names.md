# Pipeline naming — handrails the reader can skim

This applies to a **side-effecting or staged execution pipeline** (validate → run → finalize). A pure computation split into helpers (a pricing calc broken into `applyCoupon` / `applyTax`) is *not* a prepare/execute/finalize pipeline — name those by what they compute, not with pipeline verbs; forcing `prepare_`/`execute_` onto a pure calc produces lying names.

When work breaks into a pipeline, name the steps so the reader doesn't need archaeology tongs to discover the order:

- `prepare_x` → validate, apply preflight policy, return a ready-to-execute value.
- `execute_prepared_x` → only execute; capture execution failure as data.
- `finalize_executed_x` → apply post-execution overrides, encode the result.

The names form the pipeline. A reader scanning the module sees the order without reading bodies.

## Red flags

- Three helpers named `do_x_step1`, `do_x_step2`, `do_x_step3`. → The numbers carry no meaning. Name the *step*, not the position.
- A `prepare_x` that also executes. → Lying name. Either rename or split.
- A pipeline where step 2 reaches back into step 1's mutable state. → That's not a pipeline, it's a coupling. Pass the prepared value forward as data.
