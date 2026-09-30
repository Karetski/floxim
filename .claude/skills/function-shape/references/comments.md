# Comments — phase markers, not subtitles

Default: write no comment. Add one only when removing it would confuse a future reader.

- **Doc comments** on public items. State merge semantics, ordering, ownership, cancellation, and failure mode — the things callers can't infer from the type signature. Document the contract on the item that exposes it, not in a separate prose file readers won't find.
- **Inline comments** at phase transitions, edge-case policy, or external-protocol weirdness. A good inline comment answers "why this step exists" or "what invariant this block protects."
- **No comment** for code whose intent is already in the name.

In a longer function, the inline comments should form a skim-readable outline:

```text
// normalize line endings
// reject empty old-text early
// fuzzy-fallback if any literal match missed
// match each edit, fail loudly on miss or duplicate
// detect overlaps before mutating
// apply edits in reverse to keep indices stable
```

## Red flags

- A comment narrating syntax: `// loop over edits`. → Delete.
- A comment referencing the current task / PR / issue (`// added for ticket 6, case 7`). → Belongs in the commit message; comments outlive the task and stale anchors mislead the next reader.
- A doc comment that says "customizes the result" without naming the merge semantics. → Empty smoke. State the contract: which fields replace, which merge, which are ignored.
