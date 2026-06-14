"""Registry of CLI review adapters.

To add an agent: implement the `ReviewAgent` protocol in a new module and add
one entry to `_REGISTRY`. The orchestrator selects an adapter by `--agent`.
"""

from __future__ import annotations

from .base import AgentInvocation, AgentStreamError, OperationalError, ReviewAgent
from .claude import ClaudeAgent
from .codex import CodexAgent

_REGISTRY: dict[str, ReviewAgent] = {
    agent.name: agent
    for agent in (ClaudeAgent(), CodexAgent())
}


def available_agents() -> list[str]:
    return sorted(_REGISTRY)


def get_agent(name: str) -> ReviewAgent:
    agent = _REGISTRY.get(name)
    if agent is None:
        raise OperationalError(
            "invalid_input",
            f"Unknown agent '{name}'. Available agents: {', '.join(available_agents())}.",
        )
    return agent


__all__ = [
    "AgentInvocation",
    "AgentStreamError",
    "OperationalError",
    "ReviewAgent",
    "available_agents",
    "get_agent",
]
