---
name: agent-review
description: Run a peer CLI agent (Claude Code, Codex, ...) to review code, diffs, or plans when explicitly requested by the user or via $agent-review.
argument-hint: <claude|codex> [what to review]
---

# Agent Review

## Overview

Use a peer CLI agent as a reviewer, not as the authority. The goal is to surface blind spots, challenge weak reasoning, and tighten the artifact under review while keeping you responsible for the final judgment.

The reviewer is pluggable. You must select one with the required `--agent` flag — for example `--agent claude` or `--agent codex`. The workflow below is identical regardless of agent — only the `--agent` value and that agent's authentication requirement change.

## Agents

Available review agents (select with the required `--agent` flag):

- `claude` — requires an authenticated `claude` on PATH.
- `codex` — requires an authenticated `codex` on PATH.

Each agent resumes its own session by id and returns structured JSON matching the same review schema. The `session_id` returned in one round must be passed back via `--resume-session-id` on the next round, with the same `--agent`.

Optionally pick the agent's model and reasoning level with `--model` and `--reasoning`. Both are optional and independent — pass either, both, or neither. They are forwarded to the agent's CLI as-is (the accepted values differ per agent and are validated by the CLI, surfacing an `operational_error` if invalid). Pass the same values on every round.

## Workflow

0. Choose the review agent.
   The first invocation argument selects the agent: `claude` or `codex`. Pass it to the helper as `--agent <name>` on every round.
   If the agent is missing or ambiguous, stop and ask the user to choose `claude` or `codex`. Do not guess, and do not infer it from the review subject.
1. Identify the review subject.
   In Plan Mode, use the current plan text as the subject.
   Outside Plan Mode, review only a clearly identified subject from context, such as a diff, design note, issue summary, code snippet, or one or more project files.
   If multiple targets are plausible or the request is underspecified, stop and ask what the agent should review.
2. Prepare the review input you will pipe to stdin.
   For project files in the current working directory, prefer path-based review input such as `Review src/auth.py lines 40-110`.
   For non-materialized plans or discussions, either pipe the text directly or, for larger subjects, materialize them to `/tmp/...md` and refer to the path instead.
   For round 2 and later, append your response bundle after an `=== AGENT_REVIEW_RESPONSE ===` marker on its own line in the same stdin payload.
3. Run the helper script.

Round 1 — only the review input, no marker:

```bash
cat <<'EOF' | python3 scripts/agent_review.py \
  --agent claude \
  --iteration 1 \
  --max-iterations 10
Review docs/plan.md and src/reviewer.py. Focus on missing decisions and retry behavior.
EOF
```

Round 2+ — resume the session and append your response bundle after the marker on its own line:

```bash
cat <<'EOF' | python3 scripts/agent_review.py \
  --agent claude \
  --iteration 2 \
  --max-iterations 10 \
  --resume-session-id "$SESSION_ID"
Review docs/plan.md and src/reviewer.py. Focus on missing decisions and retry behavior.
=== AGENT_REVIEW_RESPONSE ===
Accepted: added retry-with-backoff to publish() (issue r1).
Rejected: issue r2 — the caller already holds the lock, so the extra mutex is redundant.
EOF
```

`--agent` is required on every round (round 1 and all resumes); use `--agent codex` to review with Codex instead.

`$SESSION_ID` is the `session_id` from the previous round's JSON output. Keep using the same `--agent` across rounds — a session id from one agent is not valid for another.
If your stdin references files outside the current working directory, pass `--add-dir` for each extra readable directory, for example `--add-dir /tmp`.
The helper defaults to a 600-second wall-clock budget for the whole round (including any JSON-repair retry). For unusually large reviews, pass `--timeout-seconds` to raise or lower that bound.

If the helper returns `{"kind":"operational_error","reason":"auth_unavailable",...}`, do not assume the user is actually logged out. In sandboxed environments, treat sandbox isolation as the likely cause and rerun the helper command with escalated execution before concluding that the agent's authentication is unavailable.

4. Read the agent's structured output and decide point by point.
   Accept useful criticism and update the artifact.
   Reject criticism that is mistaken, overspecified, or based on a wrong assumption.
   Do not defer to the agent just because it sounds confident.
   After each round, print a short progress update to the user. Format: one line per issue the agent raised this round, in the form `<short title> — accepted/fixed` or `<short title> — rejected: <one-line reason>`. Keep titles short; do not paste full rationales. If the agent raised no issues this round, say so on a single line.
   Internally, track every issue across rounds by its `id` so the final report can reconcile outcomes — the `id` is for your bookkeeping only and should not appear in user-facing lines.
5. When you disagree, prepare a response bundle for the next round.
   Include only:
   - accepted points and what changed
   - rejected points and why
   Keep the tone direct and technical.
   Re-send the full current review input every round. Resume preserves discussion state, not the latest ground-truth subject.
6. Repeat until one of these is true:
   - the agent approves or has no actionable issues
   - the same disagreement repeats after a substantive rebuttal
   - total iterations reaches 10
7. End with a final report to the user.
   Collect every issue the agent raised across all rounds, deduplicated by `id`, and group them. Use one line per issue (short title + one-line outcome note); do not include the raw `id` in user-facing lines. Omit any group that is empty — do not emit a placeholder.
   - **Fixed** — issues you accepted at any point (immediately or after discussion) and applied to the artifact.
   - **Rejected, agent withdrew** — issues you initially disagreed with and the agent dropped after your rebuttal (they did not resurface in later rounds).
   - **Unresolved** — issues where the agent still insisted and you still disagreed when the loop ended. These are what the user needs to judge.

If the helper returns `{"kind":"operational_error", ...}`, do not start or continue iterations. Report that review was not possible, show the reason and message, and stop.

## Guidance

- This skill cannot bypass permissions by itself. The Python helper only invokes the selected agent CLI. When sandboxed execution cannot access the agent's login, request escalation for the helper command instead of debugging the wrapper first.
- Use `--resume-session-id` for every round after the first, with the same `--agent`. If the session is lost, start a new review session instead of trying to reconstruct it from pasted prior feedback.
- Prefer file-path references for repo-backed subjects. Use inline text for short non-file material; for larger plans or discussions, materialize them to a temp file and review the file path instead.
- Re-send the full current review input every round even when resuming. The resumed session remembers the conversation, but the current review input is still the authoritative review target.
- Long-running reviews are normal. Lack of intermediate output is not a failure by itself unless the helper exits or the configured timeout is hit.
- Keep the review input specific. State what to review and what kind of review you want: missing decisions, correctness risks, scope control, code quality, or implementation gaps.
- Use later rounds to defend the artifact when you believe the agent is wrong. Consensus is useful, but not mandatory.
- A rejected point becomes unresolved only if the loop ends while the agent still insists on it.

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
  - `needs_changes` means the agent sees concrete changes to make
  - `discuss` means the round is mostly about disagreement or clarification
- `session_id` is the agent's conversation identifier to pass back via `--resume-session-id` on the next round (with the same `--agent`).
- `loop_signal` means the agent appears to be repeating a contested point or explicitly says consensus is unlikely soon.

## Resources

### scripts/

- `scripts/agent_review.py`: the agent-agnostic orchestrator. It reads review input from stdin, optionally splits off an `=== AGENT_REVIEW_RESPONSE ===` section on its own line for later rounds, builds the review prompt and the response schema, selects an adapter via `--agent`, runs the agent CLI, normalizes the response, and issues one session-aware JSON-repair retry if needed.
- `scripts/adapters/`: one module per CLI agent (`claude.py`, `codex.py`), each implementing the `ReviewAgent` protocol in `base.py` (build the command, extract the structured payload and session id, classify failures). `__init__.py` is the registry; add an agent by adding a module and one registry entry.
- On non-review failures, the helper exits non-zero and prints a structured `operational_error` JSON payload instead of raw stderr.
