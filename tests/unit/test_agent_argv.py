"""A13 (TODO-0313): the ``--agent`` flag only when the role names one.

opencode 2.x rejects an unknown agent name (``Error: Agent not found``),
so a role without ``models.<role>.agent_name`` must run on the default
agent — the 1.x behavior — instead of passing the role name as the agent.
"""
from __future__ import annotations

from awf.supervisor import argv_agent_flag


def test_no_agent_name_means_no_flag():
    config = {"models": {"supervisor": {"model": "vllm/llm"}}}
    assert argv_agent_flag(config, "supervisor") == []


def test_explicit_agent_name_gives_one_flag():
    config = {"models": {"supervisor": {"agent_name": "supervisor"}}}
    assert argv_agent_flag(config, "supervisor") == ["--agent", "supervisor"]


def test_empty_agent_name_is_not_a_name():
    config = {"models": {"supervisor": {"agent_name": "  "}}}
    assert argv_agent_flag(config, "supervisor") == []


def test_missing_role_means_no_flag():
    assert argv_agent_flag({}, "agent-architect-review") == []
