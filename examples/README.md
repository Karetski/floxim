# Examples

The four flows from the [spec's Appendix A](../docs/spec.md#appendix-a-example-flows), with the schemas and shared templates they use. Each is explained there in detail. They are part of Floxim's test corpus, so they always validate.

| Flow | Shows | Needs |
|---|---|---|
| [`implement-feature.yaml`](implement-feature.yaml) | Plan, implement, a test loop with a retry limit, human approval before merging, structured output with a `status` field. | Claude Code, Codex, a test command |
| [`nightly-deps.yaml`](nightly-deps.yaml) | An unattended run from cron or CI: npm upgrades in a named git worktree, a reviewer on a second harness, a pull request as the human checkpoint. | Claude Code, Codex, npm, `gh` |
| [`babysit-pr.yaml`](babysit-pr.yaml) | Polling CI with `sleep`, an agent fixing failures, asking a person when stuck, token budgets. | Codex, `gh` |
| [`triage-issues.yaml`](triage-issues.yaml) | `include`, a `python` node, `map` over a subflow ([`triage-one.yaml`](triage-one.yaml)), `set` and `notify`. | Claude Code, `gh` |

Look at one without running anything:

```sh
floxim validate examples/implement-feature.yaml
floxim graph examples/implement-feature.yaml
```

To use one, copy it with its `schemas/` (and `shared/` for the triage flows) into your project's `flows/` directory and adjust the commands, harnesses and budgets to your repository. These flows run real agents and spend money. Each run stops at its budget: the flow's `limits`, or $25 and 10 million tokens when it sets none.
