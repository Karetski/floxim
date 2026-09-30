---
name: principle-review
description: Review a substantial change against the project's own stated principles, one principle at a time; when the project has no principles doc, fall back to a built-in rubric (deep modules, least surprise, single source of truth, simplicity, predictability, verify-mechanically). Use before merging a new feature, module, or subsystem, when asked "does this fit our design" / "is this the right shape", when auditing a PR for convention alignment, or when onboarding a change into an unfamiliar codebase. Reads the design doc, walks the change against it principle by principle, and ends with a ship / revise / reject verdict.
context: fork
agent: general-purpose
---

# principle-review

A project's principles document is only useful if it is consulted.
Without an explicit invocation, principles and conventions documents
become read-once artifacts that drift out of everyone's memory. This
skill makes principle alignment a step you take before merging, not a
hope. It grounds itself in the project's own rubric first and a
built-in fallback rubric second, and it names the specialized checks
(drift, hot-path budget, deletion) a change may also warrant.

## What this skill does

1. **Locate and re-read the project's principles at the start of each
   review.** Look, in order, for the project's stated conventions:
   `docs/principles.md`, `PRINCIPLES.md`, `ARCHITECTURE.md`,
   `CONTRIBUTING.md`, `docs/playbook.md`, `CLAUDE.md`, or whatever the
   repo points to as its design rubric. The written principles are the
   rubric, not your memory of them. If the project has **no** stated
   principles, say so and fall back to the built-in rubric below (the
   lenses in step 2), plus widely-held principles: deep modules, least
   surprise, single source of truth, simplicity, predictability,
   verify-mechanically. Flag that the project would benefit from writing
   its own principles down.

2. **For the change under review, walk every principle by name.** For
   each principle the change actually engages with, write one short
   paragraph: does the change uphold it, bend it, or break it? Skip
   principles the change doesn't touch. Common lenses worth checking
   even when unstated:
   - **Performance / responsiveness budgets.** Does the change stay
     inside whatever budget the hot path is held to? If it claims a
     perf win, where is the measurement?
   - **Simplicity.** Does the change make the call site simpler or more
     complex? If it adds an abstraction, what concrete pain does it
     absorb (Ousterhout's deep-module test — small interface, powerful
     behaviour)?
   - **Predictability over cleverness.** Any new implicit dispatch,
     order-dependent registration, or context-sensitive behaviour?
     Name it.
   - **Source of truth / authority boundaries.** Does the change
     respect where authoritative state lives? Does it let an untrusted
     or downstream layer influence state it shouldn't own?
   - **Verify mechanically, not by convention.** Does the change
     introduce a rule the build cannot enforce? If yes, propose where
     the enforcement belongs (a lint config, a CI check, a codegen
     check, a golden/snapshot test). "Remember to X" comments are a
     smell.
   - **Architect from prior pain, not from prior glory.** What specific
     past pain motivates this abstraction? If the answer is vague, the
     abstraction is premature.

3. **Module-boundary direction.** Reconstruct the project's dependency
   direction (its module / package / layer DAG). If no doc states the
   intended direction, infer it from the package/layer names and structure
   (e.g. `web → service → storage`, with storage innermost) and state the
   assumption you're reviewing against. Any import that runs against that
   direction is a smell — name it with the file path.

4. **Doc-shape check.** New durable architectural decisions belong
   wherever the project keeps durable decisions (its principles doc, a
   changelog/ADR entry). They do **not** belong buried in module-level
   prose or scattered into new ad-hoc docs. If the project has
   deliberately kept its docs surface small, respect that — it is
   usually load-bearing.

5. **Run the specialized checks the change warrants.** Some domains
   deserve a dedicated pass beyond the principle walk. When the change
   touches one, do that check here (and if the matching standalone skill
   is installed, it covers the same ground in depth):
   - A duplicated contract, wire format, schema, or generated mirror →
     confirm every mirror, golden fixture, and both sides of the
     boundary moved together (skill: `wire-drift-check`).
   - A latency-critical path (tick / frame / request / inner loop) →
     confirm no blocking IO, no inner-loop allocation, and bounded work
     per iteration (skill: `hot-path-budget-audit`).
   - A request to trim, delete, or judge whether something is
     over-engineered → produce a ranked deletion list, never additions
     (skill: `simplify-ruthless`).

## Output

A principle-anchored review, one short paragraph per principle the
change actually engages with. Skip principles that don't apply. End
with a single-line verdict: ship / revise / reject.

## Relation to built-in review

This complements, rather than replaces, Claude Code's bundled `/code-review`
(correctness bugs plus reuse/simplification/efficiency cleanup). Run `/code-review`
for *defects*; run this for *design and principle alignment* against the project's
own stated rubric. They layer — the built-in does the mechanical heavy lifting, this
adds your house standards on top.

## See also

Stands alone — the skills below are optional companions, not
dependencies. The specialized checks in step 5 name three review skills
you can run when a change enters their domain, but you can perform those
checks from this file alone. The standards behind the fallback rubric:
`architecture-canon` (design), `function-shape` (per-function shape),
and `behavioral-testing` (tests) — plus the always-on coding conduct.
