# agent-review

A skill that brings in a second coding agent to review your work — a plan, some code, a diff, or a design note — so you get a second opinion without leaving your current agent.

It runs the reviewer through that agent's own command-line tool, so the reviewer can be any CLI coding agent. Currently supported:

- Claude Code (`claude`)
- Codex (`codex`)

You use it from any agent that supports skills (e.g. Claude, Codex, OpenCode, …). It adds `$agent-review`: you point it at a concrete subject, it runs the chosen reviewer on it, and your agent stays responsible for deciding what to accept, what to reject, and whether another round is worth doing.

## Install

```bash
npx skills add -g https://github.com/IlyaSemenov/agent-review-skill
```

## Usage

Ask your agent to use `$agent-review`, and say which reviewer to use — `claude` (Claude Code) or `codex` (Codex). The reviewer is required; the skill will ask if you don't name one.

```text
Use $agent-review with claude on this plan.
```

```text
Use $agent-review with codex to review the changes in src/reviewer.py and tell me which objections you agree with.
```

The same reviewer is used for every round of a review; a session belongs to one agent, so the loop does not switch agents midway. You can optionally ask for a specific model or reasoning level (e.g. "review with codex on gpt-5.5, high reasoning") — both are optional and forwarded to that agent's CLI.

## Requirements

- `python3` on your `PATH`
- the chosen reviewer's CLI, on your `PATH` and already authenticated

## How It Works

The helper script at [skills/agent-review/scripts/agent_review.py](skills/agent-review/scripts/agent_review.py) is an agent-agnostic orchestrator. It runs one review round per invocation; the calling agent drives the loop. The CLI-specific details (how to invoke the agent, how to read its output, how to classify failures) live in a per-agent adapter under [skills/agent-review/scripts/adapters/](skills/agent-review/scripts/adapters/).

Each invocation:

- asks the selected agent to review a concrete subject and find the most important problems, blind spots, or weak assumptions
- returns the agent's structured feedback plus a `session_id` for the next round

Between rounds, the calling agent:

- inspects the feedback and decides what to accept, what to reject, and what to defend
- reuses the same conversation by resuming the session (with the same reviewer) so the discussion keeps its context
- repeats only while another round is still likely to improve the review or clarify a real disagreement
- stops when the agent approves, when the remaining disagreement is clear enough that another round is not worth it, or when the configured round limit is reached
- after each round, prints a one-line-per-issue progress update (what was raised and whether it was accepted or rejected)
- at the end, reports back to the user a final summary of all issues raised across rounds, grouped into what was fixed, what was rejected and dropped, and what remains unresolved for the user to judge

By default, the agent sends review instructions through standard input. When the review refers to existing project files, the agent points the reviewer at those files directly.
When the review subject is large but not already materialized as a project file, the skill can place it in a temporary file and review that path instead.

## Notes

- The skill is explicit-only. It should run when you ask for it, not implicitly.
- In sandboxed environments, the reviewer's login may be unavailable even when the CLI works in your normal shell. In that case, the agent may need to rerun the helper with escalation.
- The helper defaults to a 600-second timeout for larger reviews.

## Development

Tests for the helper's pure functions and the adapters live in [skills/agent-review/tests/](skills/agent-review/tests/) and use pytest.

Run them with `uv` (no setup — pytest is fetched on demand):

```bash
cd skills/agent-review
uv run --with pytest python -m pytest tests/
```

Or, if you already have pytest in your environment:

```bash
cd skills/agent-review
python3 -m pytest tests/
```
