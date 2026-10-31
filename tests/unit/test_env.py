"""R5 (K5): ``OPENCODE_CONFIG_CONTENT`` is one 2.x document.

The payload the worker gets must be the form opencode 2.x reads:
top-level ``permissions`` — an ORDERED rule list where the last matching
rule wins — ``agents``, and ``mcp.servers``. The 1.x keys ``permission``,
``agent`` and ``mcp.<name>`` must never appear, and the stage's rules
(plus ``locked_deny``) must come last so a host rule cannot re-enable what
the stage forbids.
"""
from __future__ import annotations

import json
from pathlib import Path

from awf._env import awf_subprocess_env


def _project(base: Path, monkeypatch, name: str, config: str = "") -> Path:
    proj = base / name
    (proj / ".agentic").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        config or "project:\n  name: test\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg" / name))
    return proj


def _host(base: Path, monkeypatch, name: str, data: dict) -> Path:
    xdg = base / "xdg" / name
    (xdg / "opencode").mkdir(parents=True, exist_ok=True)
    path = xdg / "opencode" / "opencode.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return path


def _payload(env: dict) -> dict:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])


def _resolved(rules: list[dict], action: str) -> str | None:
    effect = None
    for rule in rules:
        if rule.get("action") == action and rule.get("resource") == "*":
            effect = rule.get("effect")
    return effect


class TestPayloadShape:
    def test_top_level_keys_are_v2(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "shape")
        _host(
            tmp_path,
            monkeypatch,
            "shape",
            {
                "mcp": {
                    "servers": {
                        "agent-workflow-ui": {"type": "local"},
                        "heavy": {"type": "local"},
                    }
                }
            },
        )
        config = _payload(
            awf_subprocess_env(
                role="agent-implementer",
                project_dir=proj,
                agent_name="worker",
                restrict_control_tools=True,
                isolate_mcp=True,
            )
        )

        assert "permission" not in config
        assert "agent" not in config
        assert isinstance(config["permissions"], list)
        assert "worker" in config["agents"]
        assert isinstance(config["agents"]["worker"]["permissions"], list)
        for rule in config["permissions"] + config["agents"]["worker"]["permissions"]:
            assert set(rule) == {"action", "resource", "effect"}
        assert "servers" in config["mcp"]

    def test_mcp_isolation_uses_servers_and_disabled(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "mcp")
        _host(
            tmp_path,
            monkeypatch,
            "mcp",
            {
                "mcp": {
                    "servers": {
                        "agent-workflow-ui": {"type": "local"},
                        "heavy": {"type": "local"},
                    }
                }
            },
        )

        config = _payload(
            awf_subprocess_env(role="worker", project_dir=proj, isolate_mcp=True)
        )

        assert config["mcp"]["servers"] == {"heavy": {"disabled": True}}
        assert "enabled" not in json.dumps(config)

    def test_v1_host_permission_is_not_normalized(self, tmp_path, monkeypatch):
        """D3: a 1.x ``permission`` mapping is not converted into rules."""
        proj = _project(tmp_path, monkeypatch, "v1")
        _host(
            tmp_path,
            monkeypatch,
            "v1",
            {"permission": {"edit": "deny", "bash": "deny"}},
        )

        config = _payload(
            awf_subprocess_env(role="agent-implementer", project_dir=proj)
        )

        actions = {rule["action"] for rule in config["permissions"]}
        assert "bash" not in actions
        assert _resolved(config["permissions"], "edit") == "allow"


class TestRuleOrder:
    def test_stage_deny_beats_host_allow(self, tmp_path, monkeypatch):
        """AC17: a stage rule comes last, so it beats the host's allow."""
        proj = _project(tmp_path, monkeypatch, "stage")
        _host(
            tmp_path,
            monkeypatch,
            "stage",
            {
                "permissions": [
                    {"action": "shell", "resource": "*", "effect": "allow"},
                    {"action": "edit", "resource": "*", "effect": "allow"},
                ]
            },
        )

        rules = _payload(
            awf_subprocess_env(
                role="agent-analyst",
                project_dir=proj,
                tools_profile={"deny": ["shell"]},
            )
        )["permissions"]

        effects = [
            (rule["action"], rule["effect"]) for rule in rules
            if rule["action"] == "shell" and rule["resource"] == "*"
        ]
        assert effects[0] == ("shell", "allow")  # host rule kept first
        assert effects[-1] == ("shell", "deny")  # stage rule last -> wins
        assert _resolved(rules, "shell") == "deny"

    def test_locked_deny_beats_any_rule(self, tmp_path, monkeypatch):
        """AC18: ``locked_deny`` is the very last rule, host allow included."""
        proj = _project(tmp_path, monkeypatch, "locked")
        _host(
            tmp_path,
            monkeypatch,
            "locked",
            {
                "permissions": [
                    {
                        "action": "agent-workflow-ui_awf_approve",
                        "resource": "*",
                        "effect": "allow",
                    }
                ]
            },
        )

        rules = _payload(
            awf_subprocess_env(
                role="agent-implementer",
                project_dir=proj,
                agent_name="worker",
                restrict_control_tools=True,
                tools_profile={"allow": ["agent-workflow-ui_awf_approve"]},
            )
        )["permissions"]

        matches = [
            rule for rule in rules
            if rule["action"] == "agent-workflow-ui_awf_approve"
        ]
        assert matches[0]["effect"] == "allow"  # host rule, first
        assert matches[-1]["effect"] == "deny"  # locked deny, after the profile
        assert _resolved(rules, "agent-workflow-ui_awf_approve") == "deny"
        # every locked rule is at the very end of the list
        assert rules[-1]["effect"] == "deny"

    def test_deny_beats_allow_inside_one_profile(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "both")
        rules = _payload(
            awf_subprocess_env(
                role="agent-implementer",
                project_dir=proj,
                tools_profile={"allow": ["webfetch"], "deny": ["webfetch"]},
            )
        )["permissions"]

        assert _resolved(rules, "webfetch") == "deny"

    def test_readonly_role_denies_edit_last(self, tmp_path, monkeypatch):
        proj = _project(
            tmp_path,
            monkeypatch,
            "readonly",
            "project:\n  name: t\nautomation:\n  readonly_roles: [agent-qa-review]\n",
        )
        rules = _payload(
            awf_subprocess_env(role="agent-qa-review", project_dir=proj)
        )["permissions"]

        assert _resolved(rules, "edit") == "deny"
        assert _resolved(rules, "shell") == "allow"


class TestToolNames:
    def test_basic_rules_use_2x_names(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "names")
        rules = _payload(
            awf_subprocess_env(role="agent-implementer", project_dir=proj)
        )["permissions"]

        actions = {rule["action"] for rule in rules}
        assert {"shell", "edit", "webfetch"} <= actions
        assert "bash" not in actions
        assert "write" not in actions

    def test_legacy_profile_keys_are_mapped(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "legacy")
        rules = _payload(
            awf_subprocess_env(
                role="agent-implementer",
                project_dir=proj,
                tools_profile={"allow": ["bash"], "deny": ["write"]},
            )
        )["permissions"]

        assert _resolved(rules, "shell") == "allow"
        assert _resolved(rules, "edit") == "deny"
        assert "bash" not in {rule["action"] for rule in rules}
        assert "write" not in {rule["action"] for rule in rules}

    def test_agent_block_denies_control_tools(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "agents")
        agent_rules = _payload(
            awf_subprocess_env(
                role="agent-implementer",
                project_dir=proj,
                agent_name="worker",
                restrict_control_tools=True,
            )
        )["agents"]["worker"]["permissions"]

        denied = {rule["action"] for rule in agent_rules if rule["effect"] == "deny"}
        assert "agent-workflow-ui_awf_approve" in denied
        assert "agent-workflow-ui_awf_kill" in denied

    def test_no_agent_block_without_restriction(self, tmp_path, monkeypatch):
        proj = _project(tmp_path, monkeypatch, "noagent")
        config = _payload(awf_subprocess_env(role="supervisor", project_dir=proj))

        assert "agents" not in config
