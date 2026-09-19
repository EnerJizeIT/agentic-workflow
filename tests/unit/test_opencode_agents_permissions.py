"""SPEC-2 (N1): the worker agent must carry a headless-safe permission profile.

A worker that hits permission=external_directory action=ask has nobody to
answer (question is disabled in awf runs) — the run dead-ends. Sanctioned
temp locations are allowed explicitly; everything else is a fast deny.
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import opencode_agents


def _cfg(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "opencode.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


def _read(cfg_path: str) -> dict:
    return json.loads(Path(cfg_path).read_text(encoding="utf-8"))


class TestWorkerPermissionProfile:
    def test_new_worker_gets_profile(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})

        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        ext = _read(cfg)["agent"]["worker"]["permission"]["external_directory"]
        assert ext["/tmp/opencode/**"] == "allow"
        assert ext["/tmp/pytest-*"] == "allow"
        assert ext["/tmp/pytest-*/**"] == "allow"

    def test_string_ask_becomes_default(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {"worker": {
            "description": "w", "model": "vllm/llm",
            "permission": {"external_directory": "ask", "webfetch": "deny"},
        }}})

        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        perm = _read(cfg)["agent"]["worker"]["permission"]
        ext = perm["external_directory"]
        assert ext["*"] == "ask"  # original intent preserved as the default
        assert ext["/tmp/opencode/**"] == "allow"
        assert perm["webfetch"] == "deny"  # untouched

    def test_string_allow_left_alone(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {"worker": {
            "description": "w", "model": "vllm/llm",
            "permission": {"external_directory": "allow"},
        }}})

        result = opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        assert "Nothing to write" in result
        assert _read(cfg)["agent"]["worker"]["permission"]["external_directory"] == "allow"

    def test_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})
        opencode_agents.apply(cfg, ["worker"], "vllm/llm")
        before = _read(cfg)

        result = opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        assert "Nothing to write" in result
        assert _read(cfg) == before

    def test_non_worker_untouched(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})

        opencode_agents.apply(cfg, ["reviewer"], "")

        assert "permission" not in _read(cfg)["agent"]["reviewer"]

    def test_propose_reports_permission_change(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {"worker": {
            "description": "w", "model": "vllm/llm",
        }}})

        proposal = opencode_agents.propose(cfg, ["worker"], "vllm/llm")

        assert proposal.kind is opencode_agents.ProposalKind.PROPOSE
        assert "external_directory allow-patterns" in proposal.detail

    def test_propose_nothing_when_profile_present(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})
        opencode_agents.apply(cfg, ["worker"], "vllm/llm")

        proposal = opencode_agents.propose(cfg, ["worker"], "vllm/llm")

        assert proposal.kind is opencode_agents.ProposalKind.NOTHING
