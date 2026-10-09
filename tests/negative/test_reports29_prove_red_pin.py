"""REPORTS29: `prove_red_pin` — first-class pin-ок for regression shields.

A pin guards EXISTING behavior: on the baseline it must be GREEN (that is
the invariant of a shield), and its teeth — that it would actually catch a
regression — are proven by mutation, OUTSIDE this tool. Before this unit
`prove_red_pin` was not a contract key (an unknown-key warning) and the
verify-pack `prove_red` section skipped it, so the supervisor had to
justify a green-on-baseline pin by hand (0160, 0171, 0168).

Each scenario builds a real git repo with a committed baseline and runs the
pack through a real `git worktree` (the same way `prove_red` does):

(a) pin + a test GREEN on the baseline  -> verdict `pin-ok`, section pass,
    exit 0;
(b) pin + a test RED on the baseline    -> section `fail` ("not a pin — the
    test falls on the old code"), exit 1;
(c) pin + a test that cannot be collected -> section `fail` (broken-runner),
    exit 1;
(d) `prove_red` and `prove_red_pin` at once -> the contract parser refuses
    (mutual exclusion).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import verify_pack as vp
from awf.unit_contract import parse_todo_contract

TODOS = "TODO-0001"

# The committed baseline is CORRECT behavior — a pin guards exactly this.
BASELINE_CALC = "def add(a, b):\n    return a + b\n"

# A pin that HOLDS on the baseline: green -> pin-ok.
GREEN_PIN = "from calc import add\n\n\ndef test_pin_add():\n    assert add(1, 2) == 3\n"
# A pin that FALLS on the baseline: red -> a finding, not a shield.
RED_PIN = "from calc import add\n\n\ndef test_pin_add():\n    assert add(1, 2) == 5\n"
# A pin that cannot be collected on the baseline (missing module) ->
# broken-runner. The module is imported but used, so ruff stays clean.
BROKEN_PIN = (
    "import reports29_no_such_mod\n\n\n"
    "def test_pin():\n"
    "    assert reports29_no_such_mod.X\n"
)

PIN_CONTRACT = '---\nprove_red_pin: ["tests/test_pin.py"]\n---\n# TODO\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _pin_repo(tmp_path: Path, contract: str, pin: str) -> Path:
    """A git repo: committed correct calc.py baseline + inbox TODO + pin test.

    The pin test lives in the working tree (untracked); `prove_red` copies
    it into the baseline worktree and runs it there.
    """
    repo = tmp_path / "pinrepo"
    (repo / ".agentic" / "inbox").mkdir(parents=True)
    (repo / ".agentic" / "context").mkdir(parents=True)
    (repo / ".agentic" / "outbox").mkdir(parents=True)
    (repo / ".gitignore").write_text(".agentic/\n__pycache__/\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    (repo / "calc.py").write_text(BASELINE_CALC)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: correct add")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (repo / ".agentic" / "context" / f"BASELINE-{TODOS}.sha").write_text(sha + "\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_pin.py").write_text(pin)
    (repo / ".agentic" / "inbox" / f"{TODOS}.md").write_text(contract)
    return repo


def _report(repo: Path) -> str:
    return (
        repo / ".agentic" / "context" / f"GATES-{TODOS}.md"
    ).read_text(encoding="utf-8")


class TestPinOk:
    """(a) green on the baseline is a pass, not a failure."""

    def test_green_on_baseline_is_pin_ok(self, tmp_path: Path) -> None:
        repo = _pin_repo(tmp_path, PIN_CONTRACT, GREEN_PIN)
        res = vp.verify_pack(repo, TODOS, tmp_base=tmp_path / "wt")

        assert res.exit_code == 0
        assert res.verdict == "ok"
        assert res.sections["prove_red"] == "pass"
        assert res.details["prove_red"] == "pin-ok"

        report = _report(repo)
        assert "pin-ok" in report
        assert "shield" in report.lower()
        assert "mutation" in report.lower()


class TestPinFailRedOnBaseline:
    """(b) red on the baseline is a FINDING, not a pin."""

    def test_red_on_baseline_is_a_finding(self, tmp_path: Path) -> None:
        repo = _pin_repo(tmp_path, PIN_CONTRACT, RED_PIN)
        res = vp.verify_pack(repo, TODOS, tmp_base=tmp_path / "wt")

        assert res.exit_code == 1
        assert res.verdict == "failed"
        assert res.sections["prove_red"] == "fail"

        report = _report(repo)
        assert "not a pin" in report.lower()


class TestPinBrokenRunner:
    """(c) a collection error on the baseline is broken-runner."""

    def test_collection_error_is_broken_runner(self, tmp_path: Path) -> None:
        repo = _pin_repo(tmp_path, PIN_CONTRACT, BROKEN_PIN)
        res = vp.verify_pack(repo, TODOS, tmp_base=tmp_path / "wt")

        assert res.exit_code == 1
        assert res.sections["prove_red"] == "fail"
        assert res.details["prove_red"] == "broken-runner"
        assert "broken-runner" in _report(repo)


class TestParserMutualExclusion:
    """(d) prove_red and prove_red_pin are mutually exclusive."""

    def test_both_keys_refused(self) -> None:
        with pytest.raises(ValueError):
            parse_todo_contract(
                '---\n'
                'prove_red: ["tests/test_pin.py::test_x"]\n'
                'prove_red_pin: ["tests/test_pin.py::test_y"]\n'
                '---\nbody\n'
            )

    def test_pin_alone_is_a_first_class_key(self) -> None:
        contract, unknown = parse_todo_contract(
            '---\nprove_red_pin: ["tests/test_pin.py::test_y"]\n---\nbody\n'
        )
        assert contract["prove_red_pin"] == ["tests/test_pin.py::test_y"]
        assert unknown == []
