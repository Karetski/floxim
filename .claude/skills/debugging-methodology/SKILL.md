---
name: debugging-methodology
description: How to diagnose a failure before touching a fix — reproduce first, form a falsifiable hypothesis, bisect to the change, and fix the root cause rather than the symptom. Use when a test fails, a crash or regression appears, behavior diverges from what you expected, an intermittent or flaky failure needs chasing, or you're tempted to add a retry / sleep / try-catch to make a symptom disappear. Ends with a fix that reproduces-then-passes and a regression test that pins it, so the same bug can't return unnoticed.
---

# debugging methodology

A bug is a gap between what you believe the code does and what it actually does. Debugging closes that gap by *observation*, not by guessing — and the fastest change that makes the symptom vanish is usually the wrong one.

Five phases. Each resists the urge to jump straight to a patch.

## 1. Reproduce first

**A fix you can't reproduce is a fix you can't verify.**

- Get a reliable, repeatable trigger before changing anything.
- Suspect a bug from *reading* the code, with no symptom in hand yet? Construct the smallest input that should trigger it and confirm the wrong result first — that constructed case is your repro.
- Intermittent? Make it deterministic first — fixed seed, forced ordering, pinned clock, single thread — then hunt the cause in the deterministic version.
- If you genuinely can't reproduce it, say so. Don't ship a speculative fix and call it done.

## 2. Hypothesize before patching

**Don't fix what you can't explain.**

- State a falsifiable claim about the cause: "this value is wrong here because `Y` runs before `Z`." Then test *that one claim*.
- A change that makes the symptom disappear without a mechanism you can name is masking, not fixing.
- Change one thing at a time. If you change five things and it works, you've learned nothing and kept four new risks.

## 3. Bisect — narrow in space and time

- **Time:** if it used to work, `git bisect` (or reading the diff since it last worked) finds the commit that introduced it, and that change usually names the bug. If it *never* worked — a latent bug, not a regression — there's nothing to bisect; go straight to the space search.
- **Space:** binary-search the pipeline — halve the input, disable half the system, drop a checkpoint at the midpoint — to localize the fault to one layer before reading everything.
- **Minimize the repro:** shrink to the smallest input or state that still triggers it. A one-line repro often *is* the diagnosis.

## 4. Observe, don't assume

- Read the actual state at the boundary — inspect the value, don't infer it. Most bugs live exactly where a belief about the data is wrong.
- A grep or symbol search doesn't prove a path *runs*; confirm it with a breakpoint, a print, or a counter.
- When the evidence and your mental story conflict, the story is wrong. Trust the evidence.

## 5. Fix the root cause, then pin it

- Fix the cause, not the nearest symptom. If the fix sits in a different layer than the cause, you're patching a leak downstream of the hole.
- Confirm the repro from phase 1 now passes *and* you can explain why.
- Add a regression test that fails without the fix. An unpinned bug returns.

## Red flags

- A retry, a sleep, a swallow-and-continue catch, or a defensive empty/null guard was added and the symptom vanished, but nobody can say *why* it was failing. → Masking. Find the mechanism.
- The bad value is patched where it surfaces — a gap collapsed, a field re-derived, a guard added a few layers downstream — instead of where it's produced. → Trace upstream and fix the origin; don't sanitize at the symptom.
- "Can't reproduce, but I think this fixes it." → Unverifiable; don't mark it done. Name what's unconfirmed.
- Five unrelated changes in one fix, one of which mattered. → You don't know which. Reduce to the single change that fixes it.
- Closed as fixed with no test that reproduces it. → The next regression ships silently. Pin it.
- A flaky test was retried into green. → Flakiness is a real bug about ordering, timing, or shared state; deflake the cause or quarantine it — don't paper over it.

## Scope

Language- and tool-neutral. Where an idiom appears — a swallowed error, a null/empty guard, `git bisect` — it is illustrative; map it to your language's and version-control system's equivalent. The five phases hold for a crash, a wrong value, or a failing test alike, and for a latent bug as much as a regression.

## See also

Stands alone — the skills below are optional companions, not dependencies: the always-on coding conduct (define a verifiable goal, then loop until it verifies), `behavioral-testing` (the regression test that pins the fix), and `wire-drift-check` (when the bug is two sides of a contract quietly disagreeing).
