"""R5 (K6, AC19/AC20): ``awf init`` and the opencode agent file.

The interactive init flow is the only place the CLI touches the agent
file. Three states, one matrix:

- no file: create it in the 2.x form, no backup (nothing to lose);
- an existing 2.x file: back it up, then add what is missing;
- a file with 1.x keys: do not change a byte, print WARN (R11).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import cmd_init


@pytest.fixture
def oc_path(tmp_path, monkeypatch):
    """Point the init flow at a temp config file (created on demand)."""
    path = tmp_path / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(cmd_init, "opencode_config_file", lambda: path)
    return path


def _answer(monkeypatch, value: str = "") -> None:
    monkeypatch.setattr("builtins.input", lambda *a, **k: value)


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


def test_missing_file_is_created_in_v2_without_backup(oc_path, monkeypatch, capsys):
    _answer(monkeypatch)

    cmd_init._offer_opencode_agent_setup()

    data = json.loads(oc_path.read_text(encoding="utf-8"))
    assert "agents" in data and "worker" in data["agents"]
    assert "mcp" in data and "servers" in data["mcp"]
    assert "agent" not in data and "permission" not in data
    assert _backups(oc_path) == []
    assert "new file" in capsys.readouterr().out


def test_existing_v2_file_is_backed_up(oc_path, monkeypatch, capsys):
    oc_path.write_text(json.dumps({"agents": {}}, indent=2), encoding="utf-8")
    before = oc_path.read_bytes()
    _answer(monkeypatch)

    cmd_init._offer_opencode_agent_setup()

    backups = _backups(oc_path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert "worker" in json.loads(oc_path.read_text(encoding="utf-8"))["agents"]
    assert "backup:" in capsys.readouterr().out


def test_v1_file_is_left_byte_for_byte(oc_path, monkeypatch, capsys):
    original = json.dumps(
        {"agent": {"worker": {}}, "permission": {"edit": "allow"}}, indent=2
    )
    oc_path.write_text(original, encoding="utf-8")
    before = oc_path.read_bytes()
    _answer(monkeypatch)

    cmd_init._offer_opencode_agent_setup()

    assert oc_path.read_bytes() == before
    assert _backups(oc_path) == []
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "сконвертируй в V2 по R11" in out
    assert "left unchanged" in out


def test_declined_answer_writes_nothing(oc_path, monkeypatch):
    _answer(monkeypatch, "n")

    cmd_init._offer_opencode_agent_setup()

    assert not oc_path.exists()
    assert _backups(oc_path) == []


def test_mcp_offer_is_v2(oc_path, monkeypatch, capsys):
    _answer(monkeypatch)
    cmd_init._add_mcp_config_to_opencode()

    servers = json.loads(oc_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert "agent-workflow-ui" in servers
    assert _backups(oc_path) == []
    assert "new file" in capsys.readouterr().out
