---
name: claude-review
description: Run Claude Code to review code, diffs, or plans when explicitly requested by the user or via $claude-review.
compatibility: Requires authenticated `claude` on PATH and `python3`. May require escalated execution in sandboxed environments.
---

# Claude Review

## Overview

Use Claude as a peer reviewer, not as the authority. The goal is to surface blind spots, challenge weak reasoning, and tighten the artifact under review while keeping you responsible for the final judgment.

## Workflow

1. Identify the review subject.
   In Plan Mode, use the current plan text as the subject.
   Outside Plan Mode, review only a clearly identified subject from context, such as a diff, design note, issue summary, code snippet, or one or more project files.
   If multiple targets are plausible or the request is underspecified, stop and ask what Claude should review.
2. Prepare the review input you will pipe to stdin.
   For project files in the current working directory, prefer path-based review input such as `Review src/auth.py lines 40-110`.
   For non-materialized plans or discussions, either pipe the text directly or, for larger subjects, materialize them to `/tmp/...md` and refer to the path instead.
   For round 2 and later, append your response bundle after an `=== CLAUDE_REVIEW_AGENT_RESPONSE ===` marker on its own line in the same stdin payload.
3. Run the helper script.

Round 1 — only the review input, no marker:

```bash
cat <<'EOF' | python3 scripts/claude_review.py \
  --iteration 1 \
  --max-iterations 10
Review docs/plan.md and src/reviewer.py. Focus on missing decisions and retry behavior.
EOF
```

Round 2+ — resume the session and append your response bundle after the marker on its own line:

```bash
cat <<'EOF' | python3 scripts/claude_review.py \
  --iteration 2 \
  --max-iterations 10 \
  --resume-session-id "$SESSION_ID"
Review docs/plan.md and src/reviewer.py. Focus on missing decisions and retry behavior.
=== CLAUDE_REVIEW_AGENT_RESPONSE ===
Accepted: added retry-with-backoff to publish() (issue r1).
Rejected: issue r2 — the caller already holds the lock, so the extra mutex is redundant.
EOF
```

`$SESSION_ID` is the `session_id` from the previous round's JSON output.
If your stdin references files outside the current working directory, pass `--add-dir` for each extra readable directory, for example `--add-dir /tmp`.
The helper defaults to a 600-second Claude subprocess timeout. For unusually large reviews, pass `--timeout-seconds` to raise or lower that bound.

If the helper returns `{"kind":"operational_error","reason":"auth_unavailable",...}`, do not assume the user is actually logged out. In sandboxed environments, treat sandbox isolation as the likely cause and rerun the helper command with escalated execution before concluding that Claude authentication is unavailable.

4. Read Claude's structured output and decide point by point.
   Accept useful criticism and update the artifact.
   Reject criticism that is mistaken, overspecified, or based on a wrong assumption.
   Do not defer to Claude just because it sounds confident.
5. When you disagree, prepare a response bundle for the next round.
   Include only:
   - accepted points and what changed
   - rejected points and why
   Keep the tone direct and technical.
   Re-send the full current review input every round. Resume preserves discussion state, not the latest ground-truth subject.
6. Repeat until one of these is true:
   - Claude approves or has no actionable issues
   - the same disagreement repeats after a substantive rebuttal
   - total iterations reaches 10
7. End with a short report.
   Include:
   - what changed from the original artifact
   - which disagreements remain unresolved at the end, if any

If the helper returns `{"kind":"operational_error", ...}`, do not start or continue iterations. Report that review was not possible, show the reason and message, and stop.

## Guidance

- This skill cannot bypass permissions by itself. The Python helper only invokes `claude`. When sandboxed execution cannot access Claude login, request escalation for the helper command instead of debugging the wrapper first.
- Use `--resume-session-id` for every round after the first. If the Claude session is lost, start a new review session instead of trying to reconstruct it from pasted prior feedback.
- Prefer file-path references for repo-backed subjects. Use inline text for short non-file material; for larger plans or discussions, materialize them to a temp file and review the file path instead.
- Re-send the full current review input every round even when resuming. The resumed session remembers the conversation, but the current review input is still the authoritative review target.
- Long-running reviews are normal. Lack of intermediate output is not a failure by itself unless the helper exits or the configured timeout is hit.
- Keep the review input specific. State what to review and what kind of review you want: missing decisions, correctness risks, scope control, code quality, or implementation gaps.
- Use later rounds to defend the artifact when you believe Claude is wrong. Consensus is useful, but not mandatory.
- A rejected point becomes unresolved only if the loop ends while Claude still insists on it.

## Script Output

The helper prints normalized JSON with this shape:

```json
{
  "session_id": "...",
  "verdict": "approve",
  "issues": [],
  "open_questions": [],
  "loop_signal": false,
  "approval_reason": "..."
}
```

- `verdict`:
  - `approve` means no actionable issues remain
  - `needs_changes` means Claude sees concrete changes to make
  - `discuss` means the round is mostly about disagreement or clarification
- `session_id` is the Claude conversation identifier to pass back via `--resume-session-id` on the next round.
- `loop_signal` means Claude appears to be repeating a contested point or explicitly says consensus is unlikely soon.

## Resources

### scripts/

- `scripts/claude_review.py`: read review input from stdin, optionally split off an `=== CLAUDE_REVIEW_AGENT_RESPONSE ===` section on its own line for later rounds, call `claude` via `$PATH`, optionally grant extra readable directories with `--add-dir`, request structured output, normalize the response, and issue one session-aware JSON repair retry if needed.
- On non-review failures, the helper exits non-zero and prints a structured `operational_error` JSON payload instead of raw stderr.
