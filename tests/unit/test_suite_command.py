"""CARRY IMP2 (R13/K14): one builder for the canonical suite command.

T3 — config wins; a missing key falls back to the pytest legacy string
with the same ``AWF_PYTHON``; null/empty/broken means "not configured";
``AWF_SUITE_CMD`` is not the resolver's business.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from awf import suite_command


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    return proj


def _write_config(proj: Path, text: str) -> None:
    (proj / ".agentic" / "config.yaml").write_text(text, encoding="utf-8")


def test_config_command_wins(tmp_path):
    proj = _project(tmp_path)
    _write_config(proj, "verification:\n  test_cmd: npm test\n")
    resolved = suite_command.resolve(proj)
    assert resolved == suite_command.SuiteCommand("npm test", "config")


def test_missing_key_uses_legacy_with_awf_python(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _write_config(proj, "verification:\n  test_cmd: null\n")
    # null is "not configured" — remove the key instead to get the legacy path.
    _write_config(proj, "verification:\n  other: 1\n")
    monkeypatch.setenv("AWF_PYTHON", "/opt/py")
    resolved = suite_command.resolve(proj)
    assert resolved is not None
    assert resolved.source == "legacy"
    assert resolved.command == (
        "/opt/py -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider"
    )


def test_no_config_file_is_legacy(tmp_path):
    proj = _project(tmp_path)
    resolved = suite_command.resolve(proj)
    assert resolved is not None
    assert resolved.source == "legacy"


@pytest.mark.parametrize(
    "text",
    [
        "verification:\n  test_cmd: null\n",
        "verification:\n  test_cmd: ''\n",
        "verification:\n  test_cmd: '   '\n",
        "verification: [broken\n",
    ],
)
def test_unset_or_broken_is_none(tmp_path, text):
    proj = _project(tmp_path)
    _write_config(proj, text)
    assert suite_command.resolve(proj) is None


def test_awf_suite_cmd_is_not_read(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _write_config(proj, "verification:\n  test_cmd: npm test\n")
    monkeypatch.setenv("AWF_SUITE_CMD", "pytest tests -k smoke")
    resolved = suite_command.resolve(proj)
    assert resolved is not None
    assert resolved.command == "npm test"


class TestCli:
    def _run(self, awf_bin, awf_env, proj, extra_env=None):
        env = dict(awf_env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [awf_bin, "suite-command", "--project-dir", str(proj)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_config_rc0(self, awf_bin, awf_env, tmp_path):
        proj = _project(tmp_path)
        _write_config(proj, "verification:\n  test_cmd: npm test\n")
        r = self._run(awf_bin, awf_env, proj)
        assert r.returncode == 0
        assert r.stdout.strip() == "npm test"

    def test_legacy_rc1(self, awf_bin, awf_env, tmp_path):
        proj = _project(tmp_path)
        r = self._run(awf_bin, awf_env, proj, {"AWF_PYTHON": "/opt/py"})
        assert r.returncode == 1
        assert r.stdout.strip().startswith("/opt/py -m pytest tests/")

    def test_unset_rc2(self, awf_bin, awf_env, tmp_path):
        proj = _project(tmp_path)
        _write_config(proj, "verification:\n  test_cmd: null\n")
        r = self._run(awf_bin, awf_env, proj)
        assert r.returncode == 2


def _git_init(proj: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)


def test_script_artifact_matches_cli(tmp_path, awf_bin):
    """T3: the script writes the same command; cmd8 is its sha256 prefix."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git_init(proj)
    (proj / ".agentic").mkdir()
    _write_config(proj, "verification:\n  test_cmd: 'true'\n")
    exclude = proj / ".git" / "info" / "exclude"
    exclude.write_text(".agentic/context/\n", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    r = subprocess.run(
        ["bash", str(Path(__file__).parents[2] / "scripts" / "suite-canonical.sh"),
         "--project", str(proj)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout

    cli = subprocess.run(
        [awf_bin, "suite-command", "--project-dir", str(proj)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    command = cli.stdout.strip()
    assert command == "true"
    cmd8 = hashlib.sha256(command.encode()).hexdigest()[:8]
    artifacts = list((proj / ".agentic" / "context").glob(f"SUITE-*-{cmd8}.json"))
    assert artifacts, "the script wrote no artifact for the canonical command"
    data = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert data["command"] == command


def test_script_carries_no_pytest_literal():
    """K14/D9: the pytest literal lives in the Python resolver only."""
    script = (Path(__file__).parents[2] / "scripts" / "suite-canonical.sh").read_text(
        encoding="utf-8"
    )
    assert "pytest tests/" not in script
