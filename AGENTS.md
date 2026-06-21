# Agent Context

`agent-review` is a skill that runs a peer CLI coding agent (Claude Code, Codex,
OpenCode) as a reviewer. The skill lives in `skills/agent-review/`.

## Commands

```bash
cd skills/agent-review
uv run --with pytest python -m pytest tests/ -v
```

## Architecture

The design is an agent-agnostic **core orchestrator** plus a **pluggable
registry of per-CLI adapters**. Adding a reviewer must not touch the core.

- `scripts/agent_review.py` — the core. Owns everything CLI-independent: stdin
  parsing (incl. the `=== AGENT_REVIEW_RESPONSE ===` marker for later rounds),
  the review prompt, the response schema, normalization/validation, the retry
  loop, timeout, and the user-facing JSON contract. It selects an adapter by
  the required `--agent` flag and runs it via `subprocess.run(input=prompt)`.
- `scripts/adapters/base.py` — the `ReviewAgent` protocol, plus
  `OperationalError`, `AgentStreamError`, and `AgentInvocation`.
- `scripts/adapters/<name>.py` — one module per CLI; `__init__.py` is the
  registry (`_REGISTRY`).

Source of truth: `RESPONSE_SCHEMA` is **derived from constants** (`OUTPUT_KEYS`,
`ISSUE_KEYS`, `VERDICTS`, `SEVERITIES`) in `agent_review.py`, and `build_prompt`
embeds `describe_schema()` (also derived from those constants) into the prompt
so the field contract and the validation can't drift.

### The adapter protocol

Each adapter implements (see `base.py`):

- `build_command(*, schema, resume_session_id, add_dirs, model, reasoning)
  -> AgentInvocation` — build the argv. Split round-1 vs resume because some
  CLIs accept a different flag set on resume. `model`/`reasoning` are passed
  through opaquely (validated by the CLI, not the core). `AgentInvocation`
  carries `argv` + `cleanup_paths` (temp files the core deletes after the run).
- `extract_payload(stdout) -> dict` — pull the review JSON out of raw stdout.
  Raise `ValueError` for an unusable payload (the core treats it as a parse
  failure and may do **one** JSON-repair retry). Raise `AgentStreamError` if the
  CLI reported a failure *in its output stream* (see gotcha below).
- `extract_session_id(stdout) -> str | None` — the resumable id.
- `resume_command(session_id) -> str` — the shell command a user runs to reopen
  the session by hand. This is the CLI's *interactive* resume form (e.g. `codex
  resume <id>`, not the `codex exec resume` the core uses), so it differs from
  the resume argv `build_command` builds. The core surfaces it (with the review's
  cwd) in the output JSON.
- `classify_failure(completed) -> OperationalError` — map a failed run onto a
  stable `reason` (`auth_unavailable` / `agent_cli_failed` / `invalid_input`).

The core never reads stdin, builds prompts, or normalizes payloads — that all
stays in `agent_review.py`. If a new *reviewer* agent tempts you to add an
`if agent ==` branch to the core, that's a signal the abstraction is leaking;
push it into the adapter or into a constants-derived, all-agents prompt addition
instead.

### Host-environment optimizations are allowed

The `if agent ==` rule above is about the **reviewer** CLIs. It does not forbid
tuning for the **host environment** that runs the skill (the calling agent, its
sandbox, its background model) — a local optimization for a known host is fine
when it: stays isolated (host *guidance* in its own doc next to `SKILL.md` like
`claude-code.md`; host *code* behind one clearly-named branch), is documented as
host-specific, and leaves the agnostic default correct for every other host. One
that can't (changes the default, or smears `if host ==` across the core) is a
leak — rework it, or keep it as guidance only.

## Adding a new adapter

1. **Research the real CLI contract first — never from docs alone.** Run the CLI
   live and capture the actual output. The facts you need:
   - non-interactive subcommand + how to get machine-readable output
   - does it read the prompt from **stdin** or only a positional arg? (the core
     feeds the prompt on stdin, so stdin support means no core change)
   - output shape: single JSON object vs **JSONL event stream**
   - where the **session id** lives, and how to **resume** by it
   - whether it can **enforce a JSON schema** CLI-side, or only be asked in the
     prompt (no-schema agents lean on `describe_schema()` + the repair retry)
   - how to set **model** and **reasoning/effort**
   - how a **failure** surfaces (exit code? stderr? an in-stream error event?)
2. Write `scripts/adapters/<name>.py` implementing `ReviewAgent`.
3. Add it to `_REGISTRY` in `scripts/adapters/__init__.py` (one line).
4. Add `tests/test_adapter_<name>.py` with fixtures built from the **real**
   captured output, not invented shapes.
5. Update docs: the agent list + `argument-hint` in `skills/agent-review/SKILL.md`,
   and the "Currently supported" list in `README.md`. These are the only two
   places that enumerate agents.

### Per-CLI contract (verified live)

All three read the prompt from **stdin**.

**claude** — `claude -p --output-format json --json-schema <inline>`; single
JSON object out; session id in the `session_id` field; model `--model`;
reasoning `--effort` (low/medium/high/xhigh/max); schema **enforced** inline.

**codex** — `codex exec --json --output-schema <file> --skip-git-repo-check
--sandbox read-only`, resume `codex exec resume <id> ...`; JSONL events out;
session id in `thread.started.thread_id`; model `--model`; reasoning
`-c model_reasoning_effort=<v>` (minimal/low/medium/high/xhigh); schema
**enforced** via `--output-schema` file.

**opencode** — `opencode run --format json`; JSONL events out; session id in
`sessionID` (on every event); model `-m provider/model`; reasoning `--variant`;
**no** schema enforcement (relies on the prompt + repair retry).

Notes that bit us:

- **codex exits 0 on API/auth/schema errors**, signalling failure only via a
  `turn.failed`/`error` event. opencode can do the same via a `{"type":"error"}`
  event. So `extract_payload` must raise `AgentStreamError` on those, and
  `classify_failure` re-parses the stream — otherwise a real failure is mistaken
  for malformed JSON and burns the repair retry. (Found by dogfooding.)
- **codex `exec resume` rejects `-s/--sandbox` and `--add-dir`** even though
  plain `codex exec` accepts them — hence the round-1-vs-resume argv split.
- codex strict schema requires `additionalProperties: false` on every object
  (our schema complies).
- **opencode has no add-dir equivalent.** `add_dirs` cannot be honored, so its
  adapter **fails fast** (`OperationalError("invalid_input", ...)`) rather than
  silently reviewing without the granted dirs — a silent drop is a false review.
- `--model`/`--reasoning` must be re-passed on **every** resume round:
  empirically codex `exec resume --model` overrides the session's model, so
  omitting it silently falls back to the default.

## Debugging gotchas

- **Host under a sandbox → `auth_unavailable`.** When the *host* agent runs the
  helper under its harness sandbox, the reviewer CLI may not reach its login even
  though the same CLI works in a normal shell, surfacing as `auth_unavailable`
  (or, with a CLI that swallows the auth error, a stuck run). This is the host's
  axis, not a reviewer bug: the host should rerun the helper with escalated
  execution before concluding the user is logged out.
- **opencode reviewer backgrounded → false `timeout`.** opencode is itself an
  agent and stalls when its own tool steps can't complete in a non-interactive
  launch; the wrapper then reports a `timeout` for a review opencode would finish
  in ~30–60s in the foreground. Reproduced with no sandbox involved (identical
  `subprocess.run` returns in ~13s foreground, times out backgrounded). The fix
  is to run the helper in the foreground — see `skills/agent-review/claude-code.md`.
  (Not observed with codex, which completed backgrounded; don't assume it
  generalizes to every reviewer.)
- **Dogfood new adapters** by running the skill on its own diff with another
  agent. It has repeatedly found real bugs (the `AgentStreamError` routing, the
  `add_dirs` false-review) that unit tests didn't.

## Doc style (SKILL.md / README)

- SKILL.md is an instruction for the *agent running the skill*; keep it to what
  drives action. Design rationale ("fails fast rather than…") belongs in code
  comments / PRs, not in SKILL.md.
- Host-specific guidance goes in its own doc next to SKILL.md (e.g.
  `claude-code.md`), not in SKILL.md itself — SKILL.md stays host-agnostic and
  links to it. Keep these short: the rule plus a one-line why, not a writeup.
- The agent list in SKILL.md is a uniform registry: identifier + human name +
  auth requirement, nothing else. Per-agent quirks (no schema enforcement,
  `--model` value format) live in the general paragraphs where they're used, not
  in the per-agent line — keep the list symmetric unless the asymmetry is real.
- Don't duplicate the agent list across sections; one source per file.
- Project names in prose are proper-cased (OpenCode, Codex); the lowercased form
  (`opencode`) is only the CLI command / `--agent` identifier.
- "…" is only for genuinely open sets (e.g. host agents that can call the skill —
  Claude, Codex, OpenCode, pi.dev, …), not for the concrete supported-reviewer
  list.
