"""The agent file is read and written in the opencode 2.x form only (R5, K6).

The worker agent must carry a headless-safe ``external_directory`` profile:
a headless worker that hits an ``ask`` prompt has nobody to answer (the
question is disabled in awf runs) and the run dead-ends. The sanctioned
temp locations are allowed explicitly, everything else is a fast deny.

2.x form: ``agents`` at the top level, a role's rules in an ordered
``permissions`` list — the ``*`` deny goes first and the sanctioned allows
last, because the last matching rule wins.
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import opencode_agents

V1_FILE = {
    "agent": {"worker": {"description": "w"}},
    "permission": {"edit": "allow"},
}


def _cfg(tmp_path: Path, data: dict, name: str = "opencode.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


def _read(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text(encoding="utf-8"))


def _effects(rules: list[dict], action: str) -> list[tuple[str, str]]:
    return [
        (rule.get("resource", "*"), rule.get("effect"))
        for rule in rules
        if rule.get("action") == action
    ]


def _backups(cfg_path: str) -> list[Path]:
    return sorted(Path(cfg_path).parent.glob(Path(cfg_path).name + ".bak-*"))


class TestWorkerPermissionProfile:
    def test_new_worker_gets_profile(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})

        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        rules = _read(cfg)["agents"]["worker"]["permissions"]
        assert rules[0] == {
            "action": "external_directory", "resource": "*", "effect": "deny",
        }
        effects = dict(_effects(rules, "external_directory"))
        assert effects["/tmp/opencode/**"] == "allow"
        assert effects["/tmp/pytest-*"] == "allow"
        assert effects["/tmp/pytest-*/**"] == "allow"
        # the allows come last: the last match wins
        assert rules[-1]["effect"] == "allow"

    def test_existing_rules_kept_deny_first(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            {
                "agents": {
                    "worker": {
                        "description": "w",
                        "model": "vllm/llm",
                        "permissions": [
                            {"action": "webfetch", "resource": "*", "effect": "deny"}
                        ],
                    }
                }
            },
        )

        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        rules = _read(cfg)["agents"]["worker"]["permissions"]
        assert {"action": "webfetch", "resource": "*", "effect": "deny"} in rules
        ext = _effects(rules, "external_directory")
        assert ext[0] == ("*", "deny")
        assert ext[-1] == ("/tmp/pytest-*/**", "allow")

    def test_full_allow_left_alone(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            {
                "agents": {
                    "worker": {
                        "description": "w",
                        "model": "vllm/llm",
                        "permissions": [
                            {
                                "action": "external_directory",
                                "resource": "*",
                                "effect": "allow",
                            }
                        ],
                    }
                }
            },
        )
        before = Path(cfg).read_text(encoding="utf-8")

        result = opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        assert "Nothing to write" in result
        assert Path(cfg).read_text(encoding="utf-8") == before
        assert _backups(cfg) == []

    def test_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})
        opencode_agents.apply(cfg, ["worker"], "vllm/llm")
        before = _read(cfg)

        result = opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        assert "Nothing to write" in result
        assert _read(cfg) == before

    def test_non_worker_untouched(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})

        opencode_agents.apply(cfg, ["reviewer"], "")

        assert "permissions" not in _read(cfg)["agents"]["reviewer"]

    def test_propose_reports_permission_change(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            {"agents": {"worker": {"description": "w", "model": "vllm/llm"}}},
        )

        proposal = opencode_agents.propose(cfg, ["worker"], "vllm/llm")

        assert proposal.kind is opencode_agents.ProposalKind.PROPOSE
        assert "external_directory allow-patterns" in proposal.detail

    def test_propose_nothing_when_profile_present(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})
        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        proposal = opencode_agents.propose(cfg, ["worker"], "vllm/llm")

        assert proposal.kind is opencode_agents.ProposalKind.NOTHING


class TestTwoForms:
    """K6 state matrix: missing file, 2.x file, 1.x file."""

    def test_missing_file_creates_v2_without_backup(self, tmp_path):
        cfg = str(tmp_path / "opencode.json")

        opencode_agents.apply(cfg, ["worker"], "")

        data = _read(cfg)
        assert "agents" in data
        assert "mcp" in data and "servers" in data["mcp"]
        assert "agent" not in data and "permission" not in data
        assert _backups(cfg) == []

    def test_v2_file_is_backed_up_before_the_write(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}, "mcp": {"servers": {}}})
        before = Path(cfg).read_bytes()

        result = opencode_agents.apply(cfg, ["worker"], "")

        backups = _backups(cfg)
        assert len(backups) == 1
        assert backups[0].read_bytes() == before
        assert "backup:" in result

    def test_v1_file_bytes_unchanged_and_warned(self, tmp_path):
        cfg = _cfg(tmp_path, V1_FILE)
        before = Path(cfg).read_bytes()

        result = opencode_agents.apply(cfg, ["worker"], "")

        assert result.strip().startswith("WARN:")
        assert "R11" in result
        assert Path(cfg).read_bytes() == before
        assert _backups(cfg) == []

    def test_propose_on_v1_file_skips(self, tmp_path):
        cfg = _cfg(tmp_path, V1_FILE)

        proposal = opencode_agents.propose(cfg, ["worker"], "")

        assert proposal.kind is opencode_agents.ProposalKind.SKIP_V1
        assert "agent" in proposal.detail
        assert "permission" in proposal.detail
        assert "R11" in proposal.detail

    def test_propose_on_missing_file_proposes_creation(self, tmp_path):
        cfg = str(tmp_path / "opencode.json")

        proposal = opencode_agents.propose(cfg, ["worker"], "")

        assert proposal.kind is opencode_agents.ProposalKind.PROPOSE
        assert proposal.to_add == ["worker"]
        assert not Path(cfg).exists()

    def test_broken_json_is_unchanged(self, tmp_path):
        cfg = str(tmp_path / "opencode.json")
        Path(cfg).write_text("{not json", encoding="utf-8")

        result = opencode_agents.apply(cfg, ["worker"], "")
        proposal = opencode_agents.propose(cfg, ["worker"], "")

        assert "ERR" in result
        assert proposal.kind is opencode_agents.ProposalKind.ERR
        assert Path(cfg).read_text(encoding="utf-8") == "{not json"
        assert _backups(cfg) == []

    def test_model_object_is_not_rewritten(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            {
                "agents": {
                    "worker": {
                        "description": "w",
                        "model": {"providerID": "vllm", "model": "llm"},
                        "permissions": [
                            {
                                "action": "external_directory",
                                "resource": "*",
                                "effect": "allow",
                            }
                        ],
                    }
                }
            },
        )

        result = opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        assert "Nothing to write" in result
        assert _read(cfg)["agents"]["worker"]["model"] == {
            "providerID": "vllm", "model": "llm",
        }


class TestMcpBlock:
    def test_adds_servers_block(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})

        result = opencode_agents.ensure_mcp_block(cfg)

        servers = _read(cfg)["mcp"]["servers"]
        assert servers["agent-workflow-ui"]["command"] == [
            "python3", "-m", "agent_workflow_ui",
        ]
        assert "backup:" in result
        assert _backups(cfg)

    def test_missing_file_creates_v2_block_without_backup(self, tmp_path):
        cfg = str(tmp_path / "opencode.json")

        result = opencode_agents.ensure_mcp_block(cfg)

        assert _read(cfg)["mcp"]["servers"]["agent-workflow-ui"]
        assert "new file" in result
        assert _backups(cfg) == []

    def test_v1_file_untouched(self, tmp_path):
        cfg = _cfg(tmp_path, {"mcp": {"agent-workflow-ui": {"type": "local"}}})
        before = Path(cfg).read_bytes()

        result = opencode_agents.ensure_mcp_block(cfg)

        assert result.startswith("WARN:")
        assert "R11" in result
        assert Path(cfg).read_bytes() == before
        assert _backups(cfg) == []

    def test_second_run_is_a_noop(self, tmp_path):
        cfg = _cfg(tmp_path, {"agents": {}})
        opencode_agents.ensure_mcp_block(cfg)
        before = Path(cfg).read_bytes()

        result = opencode_agents.ensure_mcp_block(cfg)

        assert result.startswith("OK:")
        assert Path(cfg).read_bytes() == before
