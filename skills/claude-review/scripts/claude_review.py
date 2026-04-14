#!/usr/bin/env python3
"""
Run a structured Claude review for a concrete artifact.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "verdict",
        "issues",
        "open_questions",
        "loop_signal",
        "approval_reason",
    ],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "needs_changes", "discuss"],
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "severity", "recommendation", "rationale"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "recommendation": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "loop_signal": {"type": "boolean"},
        "approval_reason": {"type": "string"},
    },
}

OUTPUT_KEYS = tuple(RESPONSE_SCHEMA["required"])
VERDICTS = {"approve", "needs_changes", "discuss"}
SEVERITIES = {"low", "medium", "high"}
DEFAULT_TIMEOUT_SECONDS = 600
MAX_PARSE_ATTEMPTS = 2
AGENT_RESPONSE_MARKER = "=== CLAUDE_REVIEW_AGENT_RESPONSE ==="


class OperationalError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

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


def extract_text_from_content_list(items: list[Any]) -> str | None:
    text_parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif item.get("type") == "text" and isinstance(item.get("content"), str):
                text_parts.append(item["content"])
    if not text_parts:
        return None
    return "\n".join(text_parts)


def looks_like_review_payload(value: Any) -> bool:
    return isinstance(value, dict) and all(key in value for key in OUTPUT_KEYS)


def unwrap_payload(value: Any) -> Any:
    if looks_like_review_payload(value):
        return value

    if isinstance(value, dict):
        for key in ("structured_output", "result", "response", "output", "message"):
            if key in value:
                candidate = unwrap_payload(value[key])
                if candidate is not None:
                    return candidate
        if "content" in value:
            candidate = unwrap_payload(value["content"])
            if candidate is not None:
                return candidate
        if "messages" in value and isinstance(value["messages"], list):
            candidate = unwrap_payload(value["messages"])
            if candidate is not None:
                return candidate

    if isinstance(value, list):
        text_blob = extract_text_from_content_list(value)
        if text_blob is not None:
            return unwrap_payload(text_blob)
        for item in value:
            candidate = unwrap_payload(item)
            if candidate is not None:
                return candidate

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return unwrap_payload(parsed)

    return None


def normalize_review(payload: Any) -> dict[str, Any]:
    if not looks_like_review_payload(payload):
        raise ValueError("Claude response does not match the expected review payload")

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


def extract_session_id(value: Any) -> str | None:
    if isinstance(value, dict):
        session_id = value.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        for nested in value.values():
            candidate = extract_session_id(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = extract_session_id(item)
            if candidate:
                return candidate
    return None


def extract_error_text(text: str) -> str:
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


def build_repair_prompt(parse_error: str) -> str:
    return "\n".join(
        [
            "Your previous response did not match the required structured JSON output.",
            f"Parsing error: {parse_error}",
            "Return valid JSON only, matching the same schema as before.",
            "Do not add commentary, markdown fences, or extra wrapper text.",
        ]
    ) + "\n"


def make_operational_error(reason: str, message: str) -> OperationalError:
    return OperationalError(reason=reason, message=message)


def classify_claude_failure(completed: subprocess.CompletedProcess[str]) -> OperationalError:
    stderr_text = extract_error_text(completed.stderr)
    stdout_text = extract_error_text(completed.stdout)
    message = stderr_text or stdout_text or "unknown error"
    if "Not logged in" in message:
        return make_operational_error(
            "auth_unavailable",
            message,
        )
    return make_operational_error(
        "claude_cli_failed",
        message,
    )


def run_claude(
    prompt: str,
    timeout_seconds: int,
    resume_session_id: str | None,
    add_dirs: list[str],
) -> str:
    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESPONSE_SCHEMA, separators=(",", ":")),
    ]
    for add_dir in add_dirs:
        command.extend(["--add-dir", add_dir])
    if resume_session_id:
        command.extend(["--resume", resume_session_id])
    completed = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise classify_claude_failure(completed)
    return completed.stdout


def request_review(
    prompt: str,
    timeout_seconds: int,
    resume_session_id: str | None,
    add_dirs: list[str],
) -> dict[str, Any]:
    last_raw_output = ""
    last_error = ""
    current_prompt = prompt
    current_resume_session_id = resume_session_id
    for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
        raw_output = run_claude(
            current_prompt, timeout_seconds, current_resume_session_id, add_dirs
        )
        last_raw_output = raw_output
        parsed: Any | None = None
        try:
            parsed = json.loads(raw_output)
            payload = unwrap_payload(parsed)
            if payload is None:
                raise ValueError("unable to locate structured review payload in Claude output")
            review = normalize_review(payload)
            session_id = extract_session_id(parsed)
            if session_id:
                review["session_id"] = session_id
            return review
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            if attempt == MAX_PARSE_ATTEMPTS:
                break
            followup_session_id = None
            if parsed is not None:
                followup_session_id = extract_session_id(parsed)
            if not followup_session_id and not current_resume_session_id:
                break
            current_resume_session_id = followup_session_id or current_resume_session_id
            current_prompt = build_repair_prompt(last_error)
    raise RuntimeError(
        "Claude returned malformed structured output after "
        f"{MAX_PARSE_ATTEMPTS} attempts: {last_error}\nRaw output:\n{last_raw_output}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a structured Claude review.")
    parser.add_argument("--iteration", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--resume-session-id")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.iteration < 1:
        raise make_operational_error("invalid_input", "--iteration must be at least 1")
    if args.max_iterations < 1:
        raise make_operational_error("invalid_input", "--max-iterations must be at least 1")
    if args.iteration > args.max_iterations:
        raise make_operational_error(
            "invalid_input", "--iteration cannot exceed --max-iterations"
        )
    if args.timeout_seconds < 1:
        raise make_operational_error("invalid_input", "--timeout-seconds must be at least 1")
    if args.iteration > 1 and not args.resume_session_id:
        raise make_operational_error(
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
    except OperationalError as exc:
        return emit_operational_error(exc)

    raw_input = sys.stdin.read()
    try:
        review_input, agent_response = parse_stdin_payload(raw_input)
    except ValueError as exc:
        return emit_operational_error(make_operational_error("invalid_input", str(exc)))

    if args.iteration > 1 and agent_response is None:
        return emit_operational_error(
            make_operational_error(
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
            prompt, args.timeout_seconds, args.resume_session_id, args.add_dir
        )
    except OperationalError as exc:
        return emit_operational_error(exc)
    except subprocess.TimeoutExpired as exc:
        return emit_operational_error(
            make_operational_error(
                "timeout",
                f"Claude review exceeded the configured timeout of {args.timeout_seconds} seconds.",
            ),
            timeout_seconds=args.timeout_seconds,
        )
    except RuntimeError as exc:
        return emit_operational_error(
            make_operational_error("claude_cli_failed", str(exc))
        )

    json.dump(review, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
