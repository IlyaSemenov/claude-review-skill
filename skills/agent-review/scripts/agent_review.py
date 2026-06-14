#!/usr/bin/env python3
"""
Run a structured peer review of a concrete artifact using a pluggable CLI agent.

The orchestration here is agent-agnostic: it parses stdin, builds the review and
repair prompts, owns the response schema and normalization, drives the retry
loop, and emits the user-facing JSON contract. Everything CLI-specific lives in
an adapter under `adapters/`, selected with `--agent`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

from adapters import (
    AgentInvocation,
    AgentStreamError,
    OperationalError,
    ReviewAgent,
    available_agents,
    get_agent,
)

# Source of truth for the review contract. The JSON schema below is derived
# from these so the enums and required keys cannot drift between the schema we
# send to the agent and the validation we run on its response.
OUTPUT_KEYS = (
    "verdict",
    "issues",
    "open_questions",
    "loop_signal",
    "approval_reason",
)
ISSUE_KEYS = ("id", "title", "severity", "recommendation", "rationale")
VERDICTS = ("approve", "needs_changes", "discuss")
SEVERITIES = ("low", "medium", "high")


def _build_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(OUTPUT_KEYS),
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(ISSUE_KEYS),
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "title": {"type": "string", "minLength": 1},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "recommendation": {"type": "string", "minLength": 1},
                        "rationale": {"type": "string", "minLength": 1},
                    },
                },
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "loop_signal": {"type": "boolean"},
            "approval_reason": {"type": "string"},
        },
    }


RESPONSE_SCHEMA = _build_response_schema()
DEFAULT_TIMEOUT_SECONDS = 600
MAX_PARSE_ATTEMPTS = 2
AGENT_RESPONSE_MARKER = "=== AGENT_REVIEW_RESPONSE ==="


def parse_stdin_payload(payload: str) -> tuple[str, str | None]:
    stripped = payload.strip()
    if not stripped:
        raise ValueError(
            "Review input is empty. Pipe review material into stdin. "
            "Use either plain review input or append an "
            f"'{AGENT_RESPONSE_MARKER}' section for later rounds."
        )

    if AGENT_RESPONSE_MARKER not in payload:
        return stripped, None

    lines = payload.splitlines()
    marker_index = next(
        (index for index, line in enumerate(lines) if line.strip() == AGENT_RESPONSE_MARKER),
        None,
    )
    if marker_index is None:
        return stripped, None

    review_input = "\n".join(lines[:marker_index]).strip()
    agent_response = "\n".join(lines[marker_index + 1 :]).strip()
    if not review_input:
        raise ValueError(
            "Sectioned stdin payload must include review input before "
            f"{AGENT_RESPONSE_MARKER}."
        )
    return review_input, (agent_response or None)


def build_prompt(
    *,
    iteration: int,
    max_iterations: int,
    review_input: str,
    agent_response: str | None,
) -> str:
    sections = [
        "You are reviewing the primary agent's work as a peer reviewer, not as the final authority.",
        "Your job is to find blind spots, weak assumptions, correctness risks, or unnecessary complexity in the supplied review input.",
        "The review input may contain inline material, file paths to inspect, or both. If it refers to files, read only the files needed for the review.",
        "Do not rewrite the entire artifact. Focus on the few highest-value issues.",
        "It is acceptable that some of your concerns may be rejected. Do not force consensus if the artifact is defensible.",
        "",
        f"Round: {iteration} of {max_iterations}",
    ]

    if agent_response is not None:
        sections.extend(
            [
                "",
                "Primary agent response to your previous feedback:",
                agent_response.strip(),
                "",
                "If you repeat a previously rejected point, add materially new reasoning or mark loop_signal true.",
            ]
        )

    sections.extend(
        [
            "",
            "Review input:",
            review_input.rstrip(),
            "",
            "Return JSON only.",
            "Choose verdict=approve only when no actionable issues remain.",
            "Choose verdict=needs_changes when the artifact should change.",
            "Choose verdict=discuss when the round is mainly about disagreement or clarification.",
            "Set loop_signal=true when you are substantially repeating a contested point or believe consensus is unlikely within the remaining rounds.",
            "Keep issues short and specific. Prefer 0-3 issues unless the artifact is seriously flawed.",
            "If there are no issues, return an empty issues array.",
        ]
    )
    return "\n".join(sections) + "\n"


def looks_like_review_payload(value: Any) -> bool:
    return isinstance(value, dict) and all(key in value for key in OUTPUT_KEYS)


def normalize_review(payload: Any) -> dict[str, Any]:
    if not looks_like_review_payload(payload):
        raise ValueError("Agent response does not match the expected review payload")

    verdict = payload["verdict"]
    if verdict not in VERDICTS:
        raise ValueError(f"Unsupported verdict: {verdict}")

    issues = payload["issues"]
    if not isinstance(issues, list):
        raise ValueError("issues must be a list")

    normalized_issues: list[dict[str, str]] = []
    for index, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            raise ValueError(f"issue #{index} must be an object")
        severity = issue.get("severity")
        if severity not in SEVERITIES:
            raise ValueError(f"issue #{index} has unsupported severity: {severity}")
        normalized_issue = {
            "id": str(issue.get("id", "")).strip(),
            "title": str(issue.get("title", "")).strip(),
            "severity": severity,
            "recommendation": str(issue.get("recommendation", "")).strip(),
            "rationale": str(issue.get("rationale", "")).strip(),
        }
        if not all(normalized_issue.values()):
            raise ValueError(f"issue #{index} has empty required fields")
        normalized_issues.append(normalized_issue)

    open_questions = payload["open_questions"]
    if not isinstance(open_questions, list) or not all(
        isinstance(item, str) for item in open_questions
    ):
        raise ValueError("open_questions must be a list of strings")

    loop_signal = payload["loop_signal"]
    if not isinstance(loop_signal, bool):
        raise ValueError("loop_signal must be a boolean")

    approval_reason = payload["approval_reason"]
    if not isinstance(approval_reason, str):
        raise ValueError("approval_reason must be a string")

    return {
        "verdict": verdict,
        "issues": normalized_issues,
        "open_questions": [item.strip() for item in open_questions if item.strip()],
        "loop_signal": loop_signal,
        "approval_reason": approval_reason.strip(),
    }


def build_repair_prompt(parse_error: str) -> str:
    return "\n".join(
        [
            "Your previous response did not match the required structured JSON output.",
            f"Parsing error: {parse_error}",
            "Return valid JSON only, matching the same schema as before.",
            "Do not add commentary, markdown fences, or extra wrapper text.",
        ]
    ) + "\n"


def run_agent(
    agent: ReviewAgent,
    prompt: str,
    timeout_seconds: int,
    resume_session_id: str | None,
    add_dirs: list[str],
    model: str | None,
    reasoning: str | None,
) -> str:
    invocation: AgentInvocation = agent.build_command(
        schema=RESPONSE_SCHEMA,
        resume_session_id=resume_session_id,
        add_dirs=add_dirs,
        model=model,
        reasoning=reasoning,
    )
    try:
        completed = subprocess.run(
            invocation.argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    finally:
        for path in invocation.cleanup_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
    if completed.returncode != 0:
        raise agent.classify_failure(completed)
    return completed.stdout


def request_review(
    agent: ReviewAgent,
    prompt: str,
    timeout_seconds: int,
    resume_session_id: str | None,
    add_dirs: list[str],
    model: str | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_raw_output = ""
    last_error = ""
    current_prompt = prompt
    current_resume_session_id = resume_session_id

    for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd=agent.name, timeout=timeout_seconds)

        raw_output = run_agent(
            agent,
            current_prompt,
            max(1, int(remaining)),
            current_resume_session_id,
            add_dirs,
            model,
            reasoning,
        )
        last_raw_output = raw_output

        followup_session_id: str | None = None
        try:
            followup_session_id = agent.extract_session_id(raw_output)
            payload = agent.extract_payload(raw_output)
            review = normalize_review(payload)
            if followup_session_id:
                review["session_id"] = followup_session_id
            return review
        except AgentStreamError:
            # The agent reported a failure in its output (and may have exited 0).
            # This is operational, not a malformed-payload parse error: route it
            # through classify_failure and stop — a repair retry is pointless.
            completed = subprocess.CompletedProcess(
                args=[agent.name], returncode=1, stdout=raw_output, stderr=""
            )
            raise agent.classify_failure(completed) from None
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)

        if attempt == MAX_PARSE_ATTEMPTS:
            break

        resume_for_retry = followup_session_id or current_resume_session_id
        if not resume_for_retry:
            break
        current_resume_session_id = resume_for_retry
        current_prompt = build_repair_prompt(last_error)

    raise RuntimeError(
        f"Agent '{agent.name}' returned malformed structured output after "
        f"{MAX_PARSE_ATTEMPTS} attempts: {last_error}\nRaw output:\n{last_raw_output}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a structured peer review with a CLI agent.")
    parser.add_argument(
        "--agent",
        required=True,
        help=f"Review agent to use ({', '.join(available_agents())}).",
    )
    parser.add_argument(
        "--model",
        help="Model for the chosen agent (passed through to its CLI). Optional.",
    )
    parser.add_argument(
        "--reasoning",
        help="Reasoning/effort level for the chosen agent (passed through to its CLI). Optional.",
    )
    parser.add_argument("--iteration", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--resume-session-id")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.iteration < 1:
        raise OperationalError("invalid_input", "--iteration must be at least 1")
    if args.max_iterations < 1:
        raise OperationalError("invalid_input", "--max-iterations must be at least 1")
    if args.iteration > args.max_iterations:
        raise OperationalError(
            "invalid_input", "--iteration cannot exceed --max-iterations"
        )
    if args.timeout_seconds < 1:
        raise OperationalError("invalid_input", "--timeout-seconds must be at least 1")
    if args.iteration > 1 and not args.resume_session_id:
        raise OperationalError(
            "invalid_input", "iteration > 1 requires --resume-session-id"
        )


def emit_operational_error(error: OperationalError, timeout_seconds: int | None = None) -> int:
    payload: dict[str, Any] = {
        "kind": "operational_error",
        "reason": error.reason,
        "message": error.message,
    }
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1


def main() -> int:
    try:
        args = parse_args()
        validate_args(args)
        agent = get_agent(args.agent)
    except OperationalError as exc:
        return emit_operational_error(exc)

    raw_input = sys.stdin.read()
    try:
        review_input, agent_response = parse_stdin_payload(raw_input)
    except ValueError as exc:
        return emit_operational_error(OperationalError("invalid_input", str(exc)))

    if args.iteration > 1 and agent_response is None:
        return emit_operational_error(
            OperationalError(
                "invalid_input",
                f"iteration > 1 requires an {AGENT_RESPONSE_MARKER} section in stdin.",
            )
        )

    prompt = build_prompt(
        iteration=args.iteration,
        max_iterations=args.max_iterations,
        review_input=review_input,
        agent_response=agent_response,
    )

    try:
        review = request_review(
            agent,
            prompt,
            args.timeout_seconds,
            args.resume_session_id,
            args.add_dir,
            args.model,
            args.reasoning,
        )
    except OperationalError as exc:
        return emit_operational_error(exc)
    except subprocess.TimeoutExpired:
        return emit_operational_error(
            OperationalError(
                "timeout",
                f"Agent '{args.agent}' review exceeded the configured timeout of {args.timeout_seconds} seconds.",
            ),
            timeout_seconds=args.timeout_seconds,
        )
    except RuntimeError as exc:
        return emit_operational_error(OperationalError("agent_cli_failed", str(exc)))

    json.dump(review, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
