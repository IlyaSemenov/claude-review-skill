"""Shared types and the adapter protocol for CLI review agents.

A review adapter encapsulates everything that is specific to one CLI agent:
how to build its command line, how to pull the structured review payload and
the resumable session id out of its output, and how to classify a failure.

The core orchestrator (`agent_review.py`) owns everything else: stdin parsing,
prompt construction, the response schema, normalization, the retry loop, and
the user-facing JSON contract.
"""

from __future__ import annotations

import subprocess
from typing import Any, Protocol, runtime_checkable


class OperationalError(Exception):
    """A non-review failure the orchestrator should surface verbatim.

    `reason` is a stable machine-readable tag; `message` is human-readable.
    Adapters raise this from `classify_failure` (and may raise it directly for
    malformed agent output).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class AgentStreamError(Exception):
    """The agent reported a failure inside its output instead of a payload.

    Some CLIs (codex) exit 0 even when the run failed, signalling the failure
    only via events in their output stream. `extract_payload` raises this so
    the orchestrator can route it through `classify_failure` rather than
    treating it as a malformed-JSON parse error and burning a repair retry.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@runtime_checkable
class ReviewAgent(Protocol):
    """Protocol every CLI review adapter implements.

    `build_command` is split into round-1 vs resume because some CLIs accept a
    different flag set when resuming (codex, for instance, rejects --sandbox on
    `exec resume`). The orchestrator calls the adapter; the adapter never reads
    stdin, builds prompts, or normalizes the review payload.
    """

    name: str

    def build_command(
        self,
        *,
        schema: dict[str, Any],
        resume_session_id: str | None,
        add_dirs: list[str],
    ) -> "AgentInvocation":
        """Return the argv (and any temp-file context) to run this round."""
        ...

    def extract_payload(self, stdout: str) -> dict[str, Any]:
        """Pull the structured review object out of the CLI's raw stdout.

        Raise ValueError if stdout does not contain a usable payload; the
        orchestrator treats that as a parse failure and may retry.
        """
        ...

    def extract_session_id(self, stdout: str) -> str | None:
        """Return the resumable session id from raw stdout, if present."""
        ...

    def classify_failure(
        self, completed: subprocess.CompletedProcess[str]
    ) -> OperationalError:
        """Map a non-zero / failed run onto an OperationalError."""
        ...


class AgentInvocation:
    """What an adapter hands back from `build_command`.

    `argv` is the command to run. `cleanup_paths` are temp files the adapter
    created (e.g. a schema written to disk) that the orchestrator must remove
    after the run. Keeping cleanup here lets the core stay adapter-agnostic.
    """

    def __init__(self, argv: list[str], cleanup_paths: list[str] | None = None) -> None:
        self.argv = argv
        self.cleanup_paths = cleanup_paths or []
