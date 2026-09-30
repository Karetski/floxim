You are planning milestone ${{ inputs.milestone }} of Arcflow.

Read CLAUDE.md, docs/milestones.md (the ${{ inputs.milestone }} section, its "Definition
of done" and its dependencies), and the spec sections it implements in docs/spec.md.
Look at the existing code before planning. Do not change any files.

Reply with:
- status: "ready", or "blocked" if the milestone depends on something missing or the
  spec is ambiguous (explain in summary);
- summary: one sentence, usable as a commit subject;
- steps: the implementation steps, tests first.
