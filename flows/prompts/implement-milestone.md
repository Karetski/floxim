${{ "Implement milestone " + inputs.milestone + " following this plan:" if visits.implement == 1 else "The last round did not pass. Fix the cause, not the tests." }}

${{ nodes.plan.output if visits.implement == 1 else "" }}
${{ "Check output:\n" + tail(nodes.check.stdout + nodes.check.stderr, 80) if nodes.check and nodes.check.exit_code != 0 else "" }}
${{ "Reviewer issues:\n" + join(nodes.review.output.issues, "\n") if nodes.review and nodes.review.output.verdict == "changes" else "" }}
${{ "Maintainer's note: " + nodes.approve.text if nodes.approve and nodes.approve.text else "" }}

Follow CLAUDE.md and .claude/conduct.md. Write tests first, keep the spec in sync, and
run `uv run pytest -q` before you finish. Do not commit.
