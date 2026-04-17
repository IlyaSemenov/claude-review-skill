# claude-review

Use Claude Code as a second-eye reviewer for plans, code, diffs, and design notes, while keeping the calling agent responsible for the final judgment.

This skill is meant to be used from another agent environment such as Codex, Cline, or pi.dev, where Claude acts as the reviewer rather than the primary executor.

## Install

```bash
npx skills add -g https://github.com/IlyaSemenov/claude-review-skill
```

## Requirements

- `claude` must be available on your `PATH`
- the Claude CLI must already be authenticated
- `python3` must be available

## What It Does

The skill adds `$claude-review`, an explicit review workflow for agents.
You ask the agent to run Claude as a reviewer on a concrete subject, then the agent decides what to accept, what to reject, and whether another review round is worth doing.

It is meant for:

- plan review
- code review
- diff review
- design-note or discussion review

## How It Works

The helper script at [skills/claude-review/scripts/claude_review.py](skills/claude-review/scripts/claude_review.py) is a thin wrapper around `claude -p`. It runs one review round per invocation; the calling agent drives the loop.

Each invocation:

- asks Claude to review a concrete subject and find the most important problems, blind spots, or weak assumptions
- returns Claude's structured feedback plus a `session_id` for the next round

Between rounds, the calling agent:

- inspects Claude's feedback and decides what to accept, what to reject, and what to defend
- reuses the same Claude conversation by passing `--resume-session-id` so the discussion keeps its context
- repeats only while another round is still likely to improve the review or clarify a real disagreement
- stops when Claude approves, when the remaining disagreement is clear enough that another round is not worth it, or when the configured round limit is reached
- reports back to the user a short summary of what changed and any unresolved disagreement for the user to judge

By default, the agent sends review instructions through standard input. When the review refers to existing project files, the agent points Claude at those files directly.
When the review subject is large but not already materialized as a project file, the skill can place it in a temporary file and point Claude at that path instead.

## Usage

After installation, ask your agent to use `$claude-review`.

Example:

```text
Use $claude-review on this plan.
```

Or:

```text
Use $claude-review to review the changes in src/reviewer.py and tell me which objections you agree with.
```

## Notes

- The skill is explicit-only. It should run when you ask for it, not implicitly.
- In sandboxed environments, Claude login may be unavailable even when `claude` works in your normal shell. In that case, the agent may need to rerun the helper with escalation.
- The helper defaults to a 600-second timeout for larger reviews.

## Development

Tests for the helper's pure functions live in [skills/claude-review/tests/](skills/claude-review/tests/) and use pytest.

Run them with `uv` (no setup — pytest is fetched on demand):

```bash
cd skills/claude-review
uv run --with pytest python -m pytest tests/
```

Or, if you already have pytest in your environment:

```bash
cd skills/claude-review
python3 -m pytest tests/
```
