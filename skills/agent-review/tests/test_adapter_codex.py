import contextlib
import json
import os
import subprocess

import pytest

from adapters import AgentStreamError, OperationalError
from adapters.codex import CodexAgent

SCHEMA = {"type": "object", "additionalProperties": False}

# Real JSONL shapes captured from `codex exec --json` (codex-cli 0.139.0).
SUCCESS_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "019ec498-f170-70b2"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": json.dumps(
                        {
                            "verdict": "approve",
                            "issues": [],
                            "open_questions": [],
                            "loop_signal": False,
                            "approval_reason": "ok",
                        }
                    ),
                },
            }
        ),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11566}}),
    ]
)

API_ERROR_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "019ec498-b98d"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "error", "message": "invalid_json_schema: bad schema"}),
        json.dumps(
            {"type": "turn.failed", "error": {"message": "invalid_json_schema: bad schema"}}
        ),
    ]
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@contextlib.contextmanager
def _build(resume_session_id=None, add_dirs=None, model=None, reasoning=None):
    inv = CodexAgent().build_command(
        schema=SCHEMA,
        resume_session_id=resume_session_id,
        add_dirs=add_dirs or [],
        model=model,
        reasoning=reasoning,
    )
    try:
        yield inv
    finally:
        for path in inv.cleanup_paths:
            if os.path.exists(path):
                os.unlink(path)


def _reasoning_override(argv):
    for i, tok in enumerate(argv):
        if tok == "-c" and argv[i + 1].startswith("model_reasoning_effort="):
            return argv[i + 1].split("=", 1)[1]
    return None


class TestBuildCommand:
    def test_round_one_includes_sandbox_and_schema_file(self):
        with _build() as inv:
            assert inv.argv[:2] == ["codex", "exec"]
            assert "resume" not in inv.argv
            assert "--sandbox" in inv.argv
            assert inv.argv[-1] == "-"
            schema_idx = inv.argv.index("--output-schema") + 1
            schema_path = inv.argv[schema_idx]
            assert schema_path in inv.cleanup_paths
            with open(schema_path) as fh:
                assert json.load(fh) == SCHEMA

    def test_round_one_add_dirs(self):
        with _build(add_dirs=["/tmp"]) as inv:
            assert inv.argv.count("--add-dir") == 1

    def test_resume_drops_sandbox(self):
        with _build(resume_session_id="sid-1", add_dirs=["/tmp"]) as inv:
            assert inv.argv[:3] == ["codex", "exec", "resume"]
            assert inv.argv[3] == "sid-1"
            # resume does not accept --sandbox or --add-dir
            assert "--sandbox" not in inv.argv
            assert "--add-dir" not in inv.argv
            assert inv.argv[-1] == "-"

    def test_no_model_or_reasoning_by_default(self):
        with _build() as inv:
            assert "--model" not in inv.argv
            assert _reasoning_override(inv.argv) is None

    def test_model_only(self):
        with _build(model="gpt-5.5") as inv:
            assert inv.argv[inv.argv.index("--model") + 1] == "gpt-5.5"
            assert _reasoning_override(inv.argv) is None

    def test_reasoning_only_maps_to_config_override(self):
        with _build(reasoning="medium") as inv:
            assert _reasoning_override(inv.argv) == "medium"
            assert "--model" not in inv.argv

    def test_model_and_reasoning_in_resume(self):
        with _build(resume_session_id="sid-1", model="gpt-5.5", reasoning="high") as inv:
            assert inv.argv[inv.argv.index("--model") + 1] == "gpt-5.5"
            assert _reasoning_override(inv.argv) == "high"
            # still ends with the stdin marker
            assert inv.argv[-1] == "-"


class TestExtractPayload:
    def test_success(self):
        payload = CodexAgent().extract_payload(SUCCESS_JSONL)
        assert payload["verdict"] == "approve"
        assert payload["approval_reason"] == "ok"

    def test_no_agent_message_raises(self):
        jsonl = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "x"}),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        with pytest.raises(ValueError, match="no agent_message"):
            CodexAgent().extract_payload(jsonl)

    def test_turn_failed_raises_stream_error(self):
        with pytest.raises(AgentStreamError, match="invalid_json_schema"):
            CodexAgent().extract_payload(API_ERROR_JSONL)

    def test_agent_message_not_json_object_raises(self):
        jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "[1,2,3]"},
            }
        )
        with pytest.raises(ValueError, match="not a JSON object"):
            CodexAgent().extract_payload(jsonl)

    def test_ignores_non_json_lines(self):
        jsonl = "garbage line\n" + SUCCESS_JSONL
        payload = CodexAgent().extract_payload(jsonl)
        assert payload["verdict"] == "approve"


class TestExtractSessionId:
    def test_from_thread_started(self):
        assert CodexAgent().extract_session_id(SUCCESS_JSONL) == "019ec498-f170-70b2"

    def test_missing(self):
        jsonl = json.dumps({"type": "turn.completed"})
        assert CodexAgent().extract_session_id(jsonl) is None

    def test_blank_output(self):
        assert CodexAgent().extract_session_id("") is None


class TestClassifyFailure:
    def test_uses_turn_failed_message(self):
        err = CodexAgent().classify_failure(_completed(stdout=API_ERROR_JSONL))
        assert isinstance(err, OperationalError)
        assert err.reason == "agent_cli_failed"
        assert "invalid_json_schema" in err.message

    def test_auth_failure_detected(self):
        jsonl = json.dumps(
            {"type": "turn.failed", "error": {"message": "401 Unauthorized"}}
        )
        err = CodexAgent().classify_failure(_completed(stdout=jsonl))
        assert err.reason == "auth_unavailable"

    def test_falls_back_to_stderr(self):
        err = CodexAgent().classify_failure(_completed(stderr="codex crashed"))
        assert err.reason == "agent_cli_failed"
        assert err.message == "codex crashed"

    def test_unknown_when_empty(self):
        err = CodexAgent().classify_failure(_completed())
        assert err.message == "unknown error"
