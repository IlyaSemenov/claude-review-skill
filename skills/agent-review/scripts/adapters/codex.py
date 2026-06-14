"""Adapter for the OpenAI Codex CLI (`codex`).

Codex differs from Claude in several ways the orchestrator must not know about:

- The schema is passed as a *file* (`--output-schema <FILE>`), not inline, so
  the adapter writes it to a temp file and reports it for cleanup.
- `--json` emits a stream of JSONL events, not one object. The resumable id
  arrives in a `thread.started` event (`thread_id`); the structured review
  arrives in an `item.completed` event whose `item.type == "agent_message"`,
  with the review JSON as a *string* under `item.text` (needs a second parse).
- Round 1 is `codex exec ...`; later rounds are `codex exec resume <id> ...`,
  which accepts a narrower flag set (no `--sandbox`, no `--add-dir`).
- The process exits 0 even on an API error; failures surface as `turn.failed`
  or `error` events in the stream, so we detect them there rather than relying
  on the return code.

Codex enforces OpenAI strict-schema rules: every object needs
`additionalProperties: false`. The orchestrator's schema already complies.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from typing import Any

from .base import AgentInvocation, AgentStreamError, OperationalError


class CodexAgent:
    name = "codex"

    def build_command(
        self,
        *,
        schema: dict[str, Any],
        resume_session_id: str | None,
        add_dirs: list[str],
        model: str | None,
        reasoning: str | None,
    ) -> AgentInvocation:
        schema_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="agent_review_codex_schema_", delete=False
        )
        try:
            json.dump(schema, schema_file)
        finally:
            schema_file.close()

        # Reasoning has no dedicated flag; codex exposes it as a config override.
        model_opts: list[str] = []
        if model:
            model_opts.extend(["--model", model])
        if reasoning:
            model_opts.extend(["-c", f"model_reasoning_effort={reasoning}"])

        if resume_session_id:
            # `exec resume` does not accept --sandbox or --add-dir.
            argv = [
                "codex",
                "exec",
                "resume",
                resume_session_id,
                "--json",
                "--output-schema",
                schema_file.name,
                "--skip-git-repo-check",
                *model_opts,
                "-",
            ]
        else:
            argv = [
                "codex",
                "exec",
                "--json",
                "--output-schema",
                schema_file.name,
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                *model_opts,
            ]
            for add_dir in add_dirs:
                argv.extend(["--add-dir", add_dir])
            argv.append("-")

        return AgentInvocation(argv, cleanup_paths=[schema_file.name])

    def extract_payload(self, stdout: str) -> dict[str, Any]:
        agent_message: str | None = None
        for event in _iter_events(stdout):
            failure = _event_failure(event)
            if failure is not None:
                # codex exits 0 on API/auth/schema failures; route this through
                # classify_failure rather than the parse-repair path.
                raise AgentStreamError(failure)
            if event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        agent_message = text

        if agent_message is None:
            raise ValueError("codex output has no agent_message item")

        payload = json.loads(agent_message)
        if not isinstance(payload, dict):
            raise ValueError("codex agent_message is not a JSON object")
        return payload

    def extract_session_id(self, stdout: str) -> str | None:
        for event in _iter_events(stdout):
            if event.get("type") == "thread.started":
                value = event.get("thread_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def classify_failure(
        self, completed: subprocess.CompletedProcess[str]
    ) -> OperationalError:
        message = _stream_failure(completed.stdout)
        if message is None:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            message = stderr or stdout or "unknown error"
        if _looks_like_auth_failure(message):
            return OperationalError("auth_unavailable", message)
        return OperationalError("agent_cli_failed", message)


def _iter_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_failure(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type == "error":
        message = event.get("message")
        return message if isinstance(message, str) else "codex error event"
    if event_type == "turn.failed":
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        return "codex turn failed"
    return None


def _stream_failure(stdout: str) -> str | None:
    for event in _iter_events(stdout):
        failure = _event_failure(event)
        if failure is not None:
            return failure
    return None


def _looks_like_auth_failure(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("not logged in", "unauthorized", "401", "login", "credentials")
    )
