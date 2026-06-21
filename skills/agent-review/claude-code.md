# Running agent-review under Claude Code

Host-agent notes for Claude Code; read alongside `SKILL.md`.

## Always run the helper in the foreground

Run the helper command in the foreground, never with `run_in_background`. This applies to every round, including `--resume-session-id` rounds.

Why: the reviewer CLI is itself an agent and calls its own tools mid-review (shell commands, permission checks). A backgrounded Bash task can't complete those steps, so the run stalls and the wrapper reports a false `{"reason":"timeout"}` even though the reviewer would finish in ~30–60s in the foreground. (If a sandbox would also deny the command, run it escalated too — but the background launch is the usual cause.)

## When you hit a `timeout`

Rerun the exact same command in the foreground first. Then:

- Do **not** raise `--timeout-seconds` — a bigger budget in the background just buys a longer false timeout.
- Do **not** report "reviewer couldn't finish" or debug the Python wrapper — the wrapper is correct; the launch was the problem.
