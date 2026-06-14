import pytest

from adapters import AgentInvocation, AgentStreamError, OperationalError
from agent_review import (
    AGENT_RESPONSE_MARKER,
    build_prompt,
    build_repair_prompt,
    looks_like_review_payload,
    normalize_review,
    parse_stdin_payload,
    request_review,
)


class TestParseStdinPayload:
    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_stdin_payload("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_stdin_payload("   \n\n  ")

    def test_plain_input_no_marker(self):
        review_input, agent_response = parse_stdin_payload("Review src/auth.py lines 40-110")
        assert review_input == "Review src/auth.py lines 40-110"
        assert agent_response is None

    def test_marker_on_own_line(self):
        payload = f"Review src/auth.py\n{AGENT_RESPONSE_MARKER}\nAgent accepts point 1."
        review_input, agent_response = parse_stdin_payload(payload)
        assert review_input == "Review src/auth.py"
        assert agent_response == "Agent accepts point 1."

    def test_marker_not_on_own_line_is_ignored(self):
        payload = f"Reviewing a doc that mentions {AGENT_RESPONSE_MARKER} inline"
        review_input, agent_response = parse_stdin_payload(payload)
        assert review_input == payload
        assert agent_response is None

    def test_marker_with_empty_input_raises(self):
        payload = f"{AGENT_RESPONSE_MARKER}\nAgent response"
        with pytest.raises(ValueError, match="must include review input before"):
            parse_stdin_payload(payload)

    def test_marker_with_empty_response_collapses_to_none(self):
        payload = f"Review\n{AGENT_RESPONSE_MARKER}\n"
        review_input, agent_response = parse_stdin_payload(payload)
        assert review_input == "Review"
        assert agent_response is None

    def test_marker_preserves_multiline_sections(self):
        payload = (
            "Review src/auth.py\nAlso review docs/plan.md\n"
            f"{AGENT_RESPONSE_MARKER}\n"
            "Accepted: r1\nRejected: r2 — bad premise"
        )
        review_input, agent_response = parse_stdin_payload(payload)
        assert review_input == "Review src/auth.py\nAlso review docs/plan.md"
        assert agent_response == "Accepted: r1\nRejected: r2 — bad premise"


class TestBuildPrompt:
    def test_first_round_has_no_agent_response_block(self):
        prompt = build_prompt(
            iteration=1,
            max_iterations=10,
            review_input="Review X",
            agent_response=None,
        )
        assert "Round: 1 of 10" in prompt
        assert "Review X" in prompt
        assert "Primary agent response" not in prompt

    def test_later_round_includes_agent_response(self):
        prompt = build_prompt(
            iteration=3,
            max_iterations=10,
            review_input="Review X",
            agent_response="Agent accepted r1.",
        )
        assert "Round: 3 of 10" in prompt
        assert "Primary agent response" in prompt
        assert "Agent accepted r1." in prompt


def _valid_payload():
    return {
        "verdict": "approve",
        "issues": [],
        "open_questions": [],
        "loop_signal": False,
        "approval_reason": "looks good",
    }


class TestLooksLikeReviewPayload:
    def test_all_keys_present(self):
        assert looks_like_review_payload(_valid_payload()) is True

    def test_missing_key(self):
        payload = _valid_payload()
        del payload["loop_signal"]
        assert looks_like_review_payload(payload) is False

    def test_not_a_dict(self):
        assert looks_like_review_payload([]) is False
        assert looks_like_review_payload("x") is False


class TestNormalizeReview:
    def test_approve_without_issues(self):
        result = normalize_review(_valid_payload())
        assert result["verdict"] == "approve"
        assert result["issues"] == []
        assert result["loop_signal"] is False
        assert "session_id" not in result

    def test_with_valid_issue(self):
        payload = _valid_payload()
        payload["verdict"] = "needs_changes"
        payload["issues"] = [
            {
                "id": "i1",
                "title": "Missing retry",
                "severity": "high",
                "recommendation": "Add retry",
                "rationale": "Network ops fail",
            }
        ]
        result = normalize_review(payload)
        assert len(result["issues"]) == 1
        assert result["issues"][0]["id"] == "i1"
        assert result["issues"][0]["severity"] == "high"

    def test_missing_key_rejected(self):
        payload = _valid_payload()
        del payload["open_questions"]
        with pytest.raises(ValueError, match="does not match"):
            normalize_review(payload)

    def test_invalid_verdict(self):
        payload = _valid_payload()
        payload["verdict"] = "maybe"
        with pytest.raises(ValueError, match="Unsupported verdict"):
            normalize_review(payload)

    def test_issue_with_bad_severity(self):
        payload = _valid_payload()
        payload["issues"] = [
            {
                "id": "x",
                "title": "t",
                "severity": "critical",
                "recommendation": "r",
                "rationale": "why",
            }
        ]
        with pytest.raises(ValueError, match="severity"):
            normalize_review(payload)

    def test_issue_with_empty_field(self):
        payload = _valid_payload()
        payload["issues"] = [
            {
                "id": "",
                "title": "t",
                "severity": "low",
                "recommendation": "r",
                "rationale": "why",
            }
        ]
        with pytest.raises(ValueError, match="empty"):
            normalize_review(payload)

    def test_loop_signal_must_be_bool(self):
        payload = _valid_payload()
        payload["loop_signal"] = "false"
        with pytest.raises(ValueError, match="loop_signal"):
            normalize_review(payload)

    def test_open_questions_must_be_strings(self):
        payload = _valid_payload()
        payload["open_questions"] = [123]
        with pytest.raises(ValueError, match="open_questions"):
            normalize_review(payload)

    def test_open_questions_strings_are_stripped_and_filtered(self):
        payload = _valid_payload()
        payload["open_questions"] = ["  one  ", "", "two"]
        result = normalize_review(payload)
        assert result["open_questions"] == ["one", "two"]


class TestBuildRepairPrompt:
    def test_includes_error(self):
        out = build_repair_prompt("bad verdict")
        assert "bad verdict" in out
        assert "valid JSON only" in out


class _StreamFailureAgent:
    """Fake adapter that exits 0 but reports a failure in its stream."""

    name = "fake"

    def __init__(self):
        self.payload_calls = 0

    def build_command(self, *, schema, resume_session_id, add_dirs, model, reasoning):
        return AgentInvocation(["true"])

    def extract_session_id(self, stdout):
        return "sid-1"

    def extract_payload(self, stdout):
        self.payload_calls += 1
        raise AgentStreamError("401 Unauthorized")

    def classify_failure(self, completed):
        return OperationalError("auth_unavailable", "401 Unauthorized")


class TestRequestReviewStreamFailure:
    def test_stream_failure_routes_through_classify_without_retry(self, monkeypatch):
        agent = _StreamFailureAgent()

        def fake_run(argv, **kwargs):
            import subprocess

            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="ignored", stderr=""
            )

        monkeypatch.setattr("agent_review.subprocess.run", fake_run)

        with pytest.raises(OperationalError) as excinfo:
            request_review(
                agent,
                prompt="p",
                timeout_seconds=30,
                resume_session_id=None,
                add_dirs=[],
            )

        assert excinfo.value.reason == "auth_unavailable"
        # No repair retry: extract_payload is called exactly once.
        assert agent.payload_calls == 1
