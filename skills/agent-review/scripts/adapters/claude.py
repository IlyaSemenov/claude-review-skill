"""Adapter for the Claude Code CLI (`claude`).

Claude returns a single JSON object on stdout that wraps the structured review
under `structured_output` and carries `session_id` for resume. The schema is
passed inline via `--json-schema`.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .base import AgentInvocation, OperationalError


class ClaudeAgent:
    name = "claude"

    def build_command(
        self,
        *,
        schema: dict[str, Any],
        resume_session_id: str | None,
        add_dirs: list[str],
        model: str | None,
        reasoning: str | None,
    ) -> AgentInvocation:
        argv = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if model:
            argv.extend(["--model", model])
        if reasoning:
            # Claude exposes reasoning level as --effort.
            argv.extend(["--effort", reasoning])
        for add_dir in add_dirs:
            argv.extend(["--add-dir", add_dir])
        if resume_session_id:
            argv.extend(["--resume", resume_session_id])
        return AgentInvocation(argv)

    def extract_payload(self, stdout: str) -> dict[str, Any]:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise ValueError("claude output is not a JSON object")
        structured_output = data.get("structured_output")
        if not isinstance(structured_output, dict):
            raise ValueError("claude output is missing 'structured_output'")
        return structured_output

    def extract_session_id(self, stdout: str) -> str | None:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def classify_failure(
        self, completed: subprocess.CompletedProcess[str]
    ) -> OperationalError:
        stderr_text = _extract_error_text(completed.stderr)
        stdout_text = _extract_error_text(completed.stdout)
        message = stderr_text or stdout_text or "unknown error"
        if "Not logged in" in message:
            return OperationalError("auth_unavailable", message)
        return OperationalError("agent_cli_failed", message)


def _extract_error_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(parsed, dict):
        for key in ("result", "error", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return stripped
