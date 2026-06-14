import pytest

from adapters import (
    OperationalError,
    ReviewAgent,
    available_agents,
    get_agent,
)


def test_available_agents_includes_claude_and_codex():
    agents = available_agents()
    assert "claude" in agents
    assert "codex" in agents


def test_get_known_agent_returns_protocol_impl():
    agent = get_agent("codex")
    assert isinstance(agent, ReviewAgent)
    assert agent.name == "codex"


def test_get_unknown_agent_raises_operational_error():
    with pytest.raises(OperationalError) as excinfo:
        get_agent("nope")
    assert excinfo.value.reason == "invalid_input"
    assert "Unknown agent" in excinfo.value.message
