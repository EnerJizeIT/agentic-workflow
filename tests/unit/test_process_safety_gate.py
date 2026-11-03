"""Process-safety gate: code calls only, prose is invisible.

The 2026-09-20 incident rule is "never signal a pid/pgid at or below 1".
The gate used to grep source text, so a docstring mentioning the forbidden
call turned it red. These tests pin both directions:

* a comment/docstring/string mentioning the call passes;
* a real call with a literal target at or below 1 fails and is reported;
* a variable target is not guessed (gate leaves it to review);
* the live tree passes (anti-vacuum) and the contract names this scanner.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "check_process_safety.py"
CONTRACT = REPO_ROOT / "docs" / "contracts" / "process-safety.md"


def _scan(tmp_path: Path, name: str, body: str) -> subprocess.CompletedProcess[str]:
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCANNER), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_prose_mentioning_the_call_passes(tmp_path):
    """Comments and docstrings never fire the gate."""
    result = _scan(
        tmp_path,
        "prose.py",
        '"""A group at or below 1 is kill(-1) territory; never call it."""\n'
        "# killpg(-1, SIGTERM) would signal the whole session\n"
        "MESSAGE = 'kill(-1, sig) is forbidden'\n"
        "VALUE = 1\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_real_call_with_literal_target_fails(tmp_path):
    """A real group signal with target -1 is a violation with file:line."""
    result = _scan(
        tmp_path,
        "bad.py",
        "import os, signal\n"
        "def boom():\n"
        "    os.killpg(-1, signal.SIGTERM)\n",
    )

    assert result.returncode == 1
    assert "bad.py:3:" in result.stdout
    assert "os.killpg(-1, signal.SIGTERM)" in result.stdout


def test_literal_one_and_bare_names_fail(tmp_path):
    """`1` is the session group too; a bare `kill` name counts."""
    one = _scan(tmp_path, "one.py", "killpg(1, 9)\n")
    zero = _scan(tmp_path, "zero.py", "from os import kill\nkill(0, 15)\n")

    assert one.returncode == 1 and "one.py:1:" in one.stdout
    assert zero.returncode == 1 and "zero.py:2:" in zero.stdout


def test_variable_target_is_not_guessed(tmp_path):
    """A dynamic target is left to review — the gate never guesses."""
    result = _scan(
        tmp_path,
        "dynamic.py",
        "import os, signal\n"
        "def kill_group(pgid):\n"
        "    os.killpg(pgid, signal.SIGTERM)\n"
        "def probe(pid):\n"
        "    os.kill(pid, 0)\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_live_tree_passes():
    """Anti-vacuum: the repository itself is clean under this scanner."""
    result = subprocess.run(
        [sys.executable, str(SCANNER), "awf", "agent_workflow_ui"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_contract_gate_uses_the_ast_scanner():
    """The contract must not regress to a grep over source text."""
    gate = next(
        line for line in CONTRACT.read_text(encoding="utf-8").splitlines()
        if line.startswith("Gate:")
    )

    assert "check_process_safety.py" in gate
    assert "grep -rnE" not in gate


def test_usage_error_is_loud():
    """No paths is a usage error (rc=2), not a silent pass."""
    result = subprocess.run(
        [sys.executable, str(SCANNER)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_keyword_target_is_a_violation(tmp_path):
    """`os.kill(pid=-1, sig=9)` is the same call, written with a keyword.

    The first version read only positional args, so the keyword form — a
    real signal to the whole session — passed the safety gate silently.
    """
    result = _scan(
        tmp_path,
        "keyword.py",
        "import os, signal\n"
        "def boom():\n"
        "    os.kill(pid=-1, sig=signal.SIGKILL)\n",
    )

    assert result.returncode == 1, result.stdout
    assert "keyword.py:3:" in result.stdout


def test_a_method_named_kill_is_not_the_signal(tmp_path):
    """`task.kill(1)` is not ``os.kill`` — the gate must not cry wolf.

    A safety gate that fires on unrelated APIs gets silenced, and a silenced
    gate finds nothing. Only ``os.kill``/``os.killpg`` (or the bare names an
    ``from os import kill`` import creates) count.
    """
    result = _scan(
        tmp_path,
        "method.py",
        "class Task:\n"
        "    def kill(self, attempt):\n"
        "        return attempt\n"
        "def use(task):\n"
        "    task.kill(1)\n",
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""
