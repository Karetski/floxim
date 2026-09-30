---
name: commit-discipline
description: How to shape commits and pull requests — atomic commits that each do one thing, refactor split from behavior change, imperative messages that explain why over what, and slices a reviewer can actually audit. Use when committing work, splitting a large change, writing a commit message or PR description, or deciding whether two changes belong in the same commit. Keeps history bisectable (every commit builds and passes) so a future git bisect lands on a real cause, and keeps diffs small enough that review is more than a rubber stamp.
---

# commit discipline

History is read far more often than it's written — by reviewers now, by `git bisect` and `git blame` later. A commit is a unit of *understanding*, not a save point.

## 1. Atomic commits

**One logical change per commit.** It should revert cleanly and stand alone.

- If the subject only fits as an "and", a comma list, a slash list, or an umbrella noun ("audit", "misc", "cleanup pass"), it's several commits wearing one hat.
- Each commit builds and passes on its own, so history stays bisectable. A commit that breaks the build hides future bugs from `git bisect`.
- Squash-merging? Atomicity moves up to the pull request: keep the PR to one concern and write the squash message like an atomic commit. A squash that flattens five concerns lands as one unreviewable commit on the main branch.

## 2. Separate refactor from behavior

- Never mix a rename / move / reformat with a logic change in one commit — the real diff drowns in the noise and the reviewer can't see it.
- Do the mechanical change in one commit, the behavior change in the next. Two small honest diffs beat one big opaque one.

## 3. Message shape

- Imperative subject that names the change ("Add retry to the uploader"), not "updates" / "fixes stuff" / "wip". If your project uses a type prefix (`feat:` / `fix:` / `test:`), the imperative sits right after it.
- Lead the body with *why* — the diff already shows what. Enumerate the *what* only as a reviewer's map when the diff is large enough to need one. Record the motivation and the tradeoff, so `git blame` answers the question a future reader actually asks.
- Write for the cold reader: durable intent, not "as discussed" or a bare ticket number. The message will be read without today's context.

## 4. Reviewable slices

- Keep a change inside what a human can actually review. A diff a reviewer can hold in their head beats a wall they wave through with "looks good".
- A PR spanning many files across unrelated subsystems is a smell — a good narrative doesn't redeem an unreviewable diff. Split it by concern and land the pieces in sequence.
- Don't bundle: unrelated fixes ride in separate commits or PRs, even when sweeping them together is tempting.

## 5. What stays out

- Generated artifacts move *with* their source, never alone — a mirror updated in isolation is drift.
- No secrets, no commented-out code, no stray debug prints, no unrelated formatting churn.
- Orphans your own change created (dead imports, unused variables) belong in the commit that orphaned them; pre-existing dead code is a separate change.

## Red flags

- A commit message reads "updates", "fixes", "wip", or "address feedback". → Say what changed and why.
- A subject that only holds together as a slash/comma list or an umbrella ("audit", "cleanup pass"). → The tell of several commits braided into one; unbraid them.
- One commit renames a module *and* changes its behavior. → Split: mechanical first, behavioral second.
- A PR touches 30+ files across subsystems. → Nobody can review it, and a narrative doesn't change that. Slice it.
- History doesn't build at some commit N (bisect-hostile). → Reorder or squash so every commit is green.
- A generated file changed but not its source, or the reverse. → Drift; regenerate and commit them together.
- "I'll clean up the history later." → Later rarely comes. Shape it now, while the intent is fresh.

## Scope

The mechanics name git — `bisect`, `blame`, squash-merge — because it's the common case; read them as your version-control system's equivalents. The discipline (atomic, refactor-split-from-behavior, reviewable, bisectable) is VCS-neutral.

## See also

Stands alone — the skills below are optional companions, not dependencies: the always-on coding conduct (surgical changes; keep output within what review can cover), `wire-drift-check` (generated mirrors move with their source), and `debugging-methodology` (a bisectable history is what lets `git bisect` land on a real cause).
