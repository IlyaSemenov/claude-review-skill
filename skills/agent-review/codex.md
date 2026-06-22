# Running agent-review under Codex

Apply these rules only when you (the host agent) are Codex.

## Confirm before the first review

The helper sends the review input (a diff, plan, or file paths — i.e. private project context) to the selected reviewer CLI, which may relay it to an external service.
Codex's approval layer treats that as an outbound transfer of workspace data and blocks the run, even on an explicit user request.
You cannot reliably tell from `--agent`/`--model` whether a given reviewer stays local, so do not try to — gate every reviewer the same way.

So before the **first** review of a session, ask the user once to confirm.
Do not ask again for later rounds of the same session.

Show, verbatim:

- the exact helper command you will run (including `--agent` and `--model`), and
- the confirmation wording the user needs to give for you to request escalation — that they explicitly authorize sending this review input to the reviewer.

Once the user gives that confirmation, request the sandbox escalation and run the exact command shown.
This does not bypass permissions — it makes the escalation explicit and auditable.

## When the run is blocked

A block here is the approval layer, not the helper or the reviewer CLI.
Do not raise `--timeout-seconds`, edit the helper, or fall back to a silent local review without telling the user.
Surface the block, quote what would be sent, and ask for the confirmation above.
