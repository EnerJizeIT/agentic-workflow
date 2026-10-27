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


# --- qa-review (0277): "no key" is legacy, even in an empty file -----------


@pytest.mark.parametrize("body", ["", "# nothing yet\n", "{}\n"])
def test_empty_or_comment_only_config_is_legacy(tmp_path, body):
    """F6: a valid YAML document with no keys has no ``test_cmd`` (K14)."""
    proj = _project(tmp_path)
    _write_config(proj, body)
    resolved = suite_command.resolve(proj)
    assert resolved is not None
    assert resolved.source == "legacy"


@pytest.mark.parametrize("name", ["pytest.toml", ".pytest.toml", ".pytest.ini"])
def test_pytest_81_config_files_are_scanned(tmp_path, name):
    """pytest 8.1+ reads these files too; their ``testpaths`` is real."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / name).write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    assert suite_command.testpaths_configured(proj)


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

    def test_unset_rc2_names_only_the_config_key(self, awf_bin, awf_env, tmp_path):
        """CTX4: this entry point never reads ``AWF_SUITE_CMD`` — do not
        advise an override it would silently ignore."""
        proj = _project(tmp_path)
        _write_config(proj, "verification:\n  test_cmd: null\n")
        r = self._run(awf_bin, awf_env, proj)
        assert r.returncode == 2
        assert "verification.test_cmd" in r.stderr
        assert "AWF_SUITE_CMD" not in r.stderr


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
    assert data["runner_source"] == "cli"


def test_script_carries_no_pytest_literal():
    """K14/D9: the pytest literal lives in the Python resolver only."""
    script = (Path(__file__).parents[2] / "scripts" / "suite-canonical.sh").read_text(
        encoding="utf-8"
    )
    assert "pytest tests/" not in script


# --- audit 0277 виток 2: AUD12 (comments are not values) ---------------------


def test_toml_comment_after_section_header(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]#pytest\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    assert suite_command.testpaths_configured(proj)


@pytest.mark.parametrize(
    "body",
    [
        '[tool.pytest.ini_options]\ntestpaths = []#none\n',
        '[tool.pytest.ini_options]\ntestpaths = [] # none\n',
    ],
)
def test_commented_empty_testpaths_is_empty(tmp_path, body):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(body, encoding="utf-8")
    assert not suite_command.testpaths_configured(proj)


def test_hash_inside_a_quoted_value_is_data(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["a#b"]\n', encoding="utf-8"
    )
    assert suite_command.testpaths_configured(proj)


# --- audit 0277 виток 3: CTX3 (ini multi-line testpaths) --------------------


@pytest.mark.parametrize(
    "body",
    [
        "[pytest]\ntestpaths =\n    tests\n    unit\n",
        "[pytest]\ntestpaths = tests\n    unit\n",
        "[pytest]\ntestpaths =\n\ttests\n\tunit\n",
    ],
)
def test_ini_multiline_testpaths(tmp_path, body):
    """CTX3: iniconfig continues an indented value onto the next lines."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text(body, encoding="utf-8")
    assert suite_command.testpaths_configured(proj)


def test_ini_continuation_stops_at_the_next_key(tmp_path):
    """CTX3: an unindented key ends the value — no false ``testpaths``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text(
        "[pytest]\ntestpaths =\naddopts = -q\n", encoding="utf-8"
    )
    assert not suite_command.testpaths_configured(proj)


def test_ini_empty_testpaths_with_comment(tmp_path):
    """CTX3: a commented empty value stays empty."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text(
        "[pytest]\ntestpaths = # none\naddopts = -q\n", encoding="utf-8"
    )
    assert not suite_command.testpaths_configured(proj)


# --- architect-review (0277): the ini continuation rule is ini-only ---------


def test_toml_indented_next_key_is_not_a_continuation(tmp_path):
    """TOML has no significant indentation: an indented ``addopts`` after a
    closed, empty ``testpaths`` is a new key, not part of the value."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "    testpaths = []\n"
        '    addopts = ["-q"]\n',
        encoding="utf-8",
    )
    assert not suite_command.testpaths_configured(proj)


def test_toml_multiline_array_still_counts(tmp_path):
    """A TOML array that closes on a later line is a real value (brackets,
    not indentation, decide)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\n    \"tests\",\n]\n",
        encoding="utf-8",
    )
    assert suite_command.testpaths_configured(proj)


# --- CTX1 (volut 4): «broken config» is its own exit code -------------------
#
# K14 keeps two states apart: the file cannot be read (nothing can be
# trusted, the legacy pytest fallback is not safe) and the key is empty
# (a deliberate "not configured"). They shared rc=2 and one message.


def test_broken_config_has_its_own_state(tmp_path):
    proj = _project(tmp_path)
    _write_config(proj, "verification: [broken\n")
    resolved, state = suite_command.resolve_with_state(proj)
    assert resolved is None
    assert state == "broken"


@pytest.mark.parametrize(
    "text",
    [
        "verification:\n  test_cmd: null\n",
        "verification:\n  test_cmd: ''\n",
        "verification:\n  test_cmd: 42\n",
    ],
)
def test_unset_config_is_not_broken(tmp_path, text):
    proj = _project(tmp_path)
    _write_config(proj, text)
    resolved, state = suite_command.resolve_with_state(proj)
    assert resolved is None
    assert state == "unset"


class TestBrokenConfigCli:
    def test_broken_config_rc5_and_message(self, awf_bin, awf_env, tmp_path):
        """CTX1: rc=5 and the exact «broken» wording, not the empty-key one."""
        proj = _project(tmp_path)
        _write_config(proj, "verification: [broken\n")
        r = subprocess.run(
            [awf_bin, "suite-command", "--project-dir", str(proj)],
            capture_output=True,
            text=True,
            env=dict(awf_env),
            check=False,
        )
        assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)
        assert "suite not configured: broken .agentic/config.yaml" in r.stderr
        assert r.stdout == ""

    def test_empty_value_stays_rc2(self, awf_bin, awf_env, tmp_path):
        proj = _project(tmp_path)
        _write_config(proj, "verification:\n  test_cmd: ''\n")
        r = subprocess.run(
            [awf_bin, "suite-command", "--project-dir", str(proj)],
            capture_output=True,
            text=True,
            env=dict(awf_env),
            check=False,
        )
        assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
        assert "broken" not in r.stderr
