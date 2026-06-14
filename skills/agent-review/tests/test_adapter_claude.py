import json
import subprocess

import pytest

from adapters import OperationalError
from adapters.claude import ClaudeAgent, _extract_error_text

SCHEMA = {"type": "object"}


def _completed(returncode=1, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestBuildCommand:
    def test_round_one(self):
        inv = ClaudeAgent().build_command(
            schema=SCHEMA, resume_session_id=None, add_dirs=[]
        )
        assert inv.argv[:2] == ["claude", "-p"]
        assert "--json-schema" in inv.argv
        assert "--resume" not in inv.argv
        assert inv.cleanup_paths == []

    def test_resume_and_add_dirs(self):
        inv = ClaudeAgent().build_command(
            schema=SCHEMA, resume_session_id="s1", add_dirs=["/tmp", "/var"]
        )
        assert "--resume" in inv.argv
        assert inv.argv[inv.argv.index("--resume") + 1] == "s1"
        assert inv.argv.count("--add-dir") == 2


class TestExtractPayload:
    def test_structured_output_extracted(self):
        payload = {"verdict": "approve"}
        raw = json.dumps({"session_id": "s1", "structured_output": payload})
        assert ClaudeAgent().extract_payload(raw) == payload

    def test_missing_structured_output_raises(self):
        raw = json.dumps({"session_id": "s1", "result": ""})
        with pytest.raises(ValueError, match="structured_output"):
            ClaudeAgent().extract_payload(raw)

    def test_non_dict_structured_output_raises(self):
        raw = json.dumps({"structured_output": None})
        with pytest.raises(ValueError, match="structured_output"):
            ClaudeAgent().extract_payload(raw)

    def test_top_level_not_object_raises(self):
        with pytest.raises(ValueError, match="not a JSON object"):
            ClaudeAgent().extract_payload(json.dumps([1, 2, 3]))

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            ClaudeAgent().extract_payload("not json")


class TestExtractSessionId:
    def test_present(self):
        raw = json.dumps({"session_id": "abc"})
        assert ClaudeAgent().extract_session_id(raw) == "abc"

    def test_stripped(self):
        raw = json.dumps({"session_id": "  abc  "})
        assert ClaudeAgent().extract_session_id(raw) == "abc"

    def test_missing(self):
        assert ClaudeAgent().extract_session_id(json.dumps({})) is None

    def test_empty_string(self):
        assert ClaudeAgent().extract_session_id(json.dumps({"session_id": ""})) is None

    def test_non_string(self):
        assert ClaudeAgent().extract_session_id(json.dumps({"session_id": 123})) is None

    def test_invalid_json(self):
        assert ClaudeAgent().extract_session_id("not json") is None


class TestClassifyFailure:
    def test_auth_failure(self):
        err = ClaudeAgent().classify_failure(
            _completed(stderr="Error: Not logged in")
        )
        assert isinstance(err, OperationalError)
        assert err.reason == "auth_unavailable"

    def test_generic_failure(self):
        err = ClaudeAgent().classify_failure(_completed(stderr="boom"))
        assert err.reason == "agent_cli_failed"
        assert err.message == "boom"

    def test_unknown_error_when_empty(self):
        err = ClaudeAgent().classify_failure(_completed())
        assert err.message == "unknown error"


class TestExtractErrorText:
    def test_plain_text(self):
        assert _extract_error_text("boom\n") == "boom"

    def test_empty(self):
        assert _extract_error_text("  \n") == ""

    def test_json_with_error_key(self):
        assert _extract_error_text(json.dumps({"error": "auth failed"})) == "auth failed"

    def test_json_with_result_key(self):
        assert _extract_error_text(json.dumps({"result": "explained"})) == "explained"

    def test_json_without_known_keys_falls_back_to_raw(self):
        raw = json.dumps({"other": "thing"})
        assert _extract_error_text(raw) == raw
