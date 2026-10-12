"""TODO-0201: prove-red — a coverage unit as a first class.

A coverage/validation unit's tests check behavior implemented EARLIER
(other units), so on this unit's baseline they are green by
construction and prove-red honestly returns not-red. Before this unit
the gate demanded a red on the baseline for such units, so the
supervisor accepted the not-red by hand (two manual bypasses in the
jql project; feature report awf-feature-20261011).

Now:

(a) a declared `prove_red_kind: coverage` + `prove_red_note`: not-red
    is a PASS of the verify-pack `prove_red` section (annotated, with
    the note in the report), pack exit 0;
(b) without the declaration: not-red is still a FAIL, as before;
(c) kind without note (and note without kind, and the kind without a
    prove_red list) — a contract error;
(d) an undeclared not-red on a TESTS-ONLY diff gets a hint line in the
    GATES report (no auto-substitution: the verdict still fails); a
    source change in the diff suppresses the hint;
(e) prove_red_kind: coverage and prove_red_pin are mutually exclusive.

Each pack scenario builds a real git repo with a committed baseline and
runs the pack through a real git worktree (the same way prove_red
does), so the not-red is measured, not mocked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import verify_pack as vp
from awf.unit_contract import parse_todo_contract

T = "TODO-0001"

# The committed baseline is CORRECT behavior — a coverage unit's tests
# guard exactly this already-implemented behavior, so they are green on
# the baseline by construction (not-red).
BASELINE_CALC = "def add(a, b):\n    return a + b\n"
COVERAGE_TEST = (
    "from calc import add\n\n\n"
    "def test_cov_add():\n    assert add(1, 2) == 3\n"
)

NOTE = "add внедрено в baseline; юнит только добавляет покрытие"

COV_CONTRACT = (
    "---\n"
    'prove_red: ["tests/test_cov.py"]\n'
    "prove_red_kind: coverage\n"
    f'prove_red_note: "{NOTE}"\n'
    "---\n# TODO\n"
)
PLAIN_CONTRACT = '---\nprove_red: ["tests/test_cov.py"]\n---\n# TODO\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _cov_repo(tmp_path: Path, contract: str, *, source_change: bool = False) -> Path:
    """A git repo: committed correct calc.py baseline + inbox TODO + the
    coverage test (untracked, working tree only).

    By default the working tree changes ONLY the test file (a tests-only
    diff); ``source_change`` also touches calc.py, to break the hint's
    tests-only precondition.
    """
    repo = tmp_path / "covrepo"
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
    (repo / ".agentic" / "context" / f"BASELINE-{T}.sha").write_text(sha + "\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_cov.py").write_text(COVERAGE_TEST)
    if source_change:
        (repo / "calc.py").write_text(BASELINE_CALC + "# touched\n")
    (repo / ".agentic" / "inbox" / f"{T}.md").write_text(contract)
    return repo


def _report(repo: Path) -> str:
    return (
        repo / ".agentic" / "context" / f"GATES-{T}.md"
    ).read_text(encoding="utf-8")


class TestDeclaredCoverage:
    """(a) declared kind + note: not-red is a pass, note in the report."""

    def test_not_red_is_ok_with_note(self, tmp_path: Path) -> None:
        repo = _cov_repo(tmp_path, COV_CONTRACT)
        res = vp.verify_pack(repo, T, tmp_base=tmp_path / "wt")

        assert res.exit_code == 0
        assert res.verdict == "ok"
        assert res.sections["prove_red"] == "pass"
        assert res.details["prove_red"] == "coverage-ok (declared not-red)"

        report = _report(repo)
        assert "coverage unit — not-red is expected (declared" in report
        assert NOTE in report

    def test_declared_kind_exempts_nothing_else(self, tmp_path: Path) -> None:
        # (a) boundary: the declaration exempts only not-red. A unit whose
        # test is RED on the baseline proves a real fix (red-ok) and must
        # pass through the NORMAL path — no coverage annotation.
        repo = tmp_path / "covrepo2"
        (repo / ".agentic" / "inbox").mkdir(parents=True)
        (repo / ".agentic" / "context").mkdir(parents=True)
        (repo / ".agentic" / "outbox").mkdir(parents=True)
        (repo / ".gitignore").write_text(".agentic/\n__pycache__/\n")
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "tester")
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "baseline: broken add")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        (repo / ".agentic" / "context" / f"BASELINE-{T}.sha").write_text(sha + "\n")
        (repo / "calc.py").write_text(BASELINE_CALC)  # the fix, working tree
        (repo / "tests").mkdir()
        (repo / "tests" / "test_cov.py").write_text(COVERAGE_TEST)
        (repo / ".agentic" / "inbox" / f"{T}.md").write_text(COV_CONTRACT)

        res = vp.verify_pack(repo, T, tmp_base=tmp_path / "wt")

        assert res.exit_code == 0
        assert res.verdict == "ok"
        assert res.sections["prove_red"] == "pass"
        assert res.details["prove_red"] == "verdict red-ok"
        assert "coverage unit — not-red is expected" not in _report(repo)


class TestUndeclaredNotRed:
    """(b) without the declaration not-red is still a fail — as before."""

    def test_not_red_without_declaration_fails(self, tmp_path: Path) -> None:
        repo = _cov_repo(tmp_path, PLAIN_CONTRACT)
        res = vp.verify_pack(repo, T, tmp_base=tmp_path / "wt")

        assert res.exit_code == 1
        assert res.verdict == "failed"
        assert res.sections["prove_red"] == "fail"
        assert res.details["prove_red"] == "verdict not-red"
        assert "coverage unit — not-red is expected" not in _report(repo)


class TestTestsOnlyHint:
    """(d) an undeclared not-red on a tests-only diff gets a hint line —
    the verdict is NOT auto-substituted (still fails)."""

    def test_tests_only_diff_gets_a_hint(self, tmp_path: Path) -> None:
        repo = _cov_repo(tmp_path, PLAIN_CONTRACT)
        res = vp.verify_pack(repo, T, tmp_base=tmp_path / "wt")

        assert res.sections["prove_red"] == "fail"  # the hint does not flip it
        report = _report(repo)
        assert "looks like a coverage unit" in report
        assert "prove_red_kind: coverage" in report

    def test_source_change_suppresses_the_hint(self, tmp_path: Path) -> None:
        repo = _cov_repo(tmp_path, PLAIN_CONTRACT, source_change=True)
        res = vp.verify_pack(repo, T, tmp_base=tmp_path / "wt")

        assert res.sections["prove_red"] == "fail"
        assert "looks like a coverage unit" not in _report(repo)


class TestContractPair:
    """(c) kind and note are a pair, and the kind needs prove_red."""

    def test_kind_without_note_is_a_contract_error(self) -> None:
        with pytest.raises(ValueError, match="requires 'prove_red_note'"):
            parse_todo_contract(
                '---\n'
                'prove_red: ["tests/test_cov.py"]\n'
                "prove_red_kind: coverage\n"
                "---\nbody\n"
            )

    def test_note_without_kind_is_a_contract_error(self) -> None:
        with pytest.raises(ValueError, match="without 'prove_red_kind'"):
            parse_todo_contract(
                '---\n'
                'prove_red: ["tests/test_cov.py"]\n'
                'prove_red_note: "why"\n'
                "---\nbody\n"
            )

    def test_kind_without_prove_red_is_a_contract_error(self) -> None:
        with pytest.raises(ValueError, match="requires a 'prove_red' list"):
            parse_todo_contract(
                '---\n'
                "prove_red_kind: coverage\n"
                'prove_red_note: "why"\n'
                "---\nbody\n"
            )

    def test_unknown_kind_is_a_contract_error(self) -> None:
        with pytest.raises(ValueError, match="unknown prove_red_kind"):
            parse_todo_contract(
                '---\n'
                'prove_red: ["tests/test_cov.py"]\n'
                "prove_red_kind: validation\n"
                'prove_red_note: "why"\n'
                "---\nbody\n"
            )

    def test_valid_pair_is_a_first_class_key(self) -> None:
        contract, unknown = parse_todo_contract(
            '---\n'
            'prove_red: ["tests/test_cov.py"]\n'
            "prove_red_kind: coverage\n"
            f'prove_red_note: "{NOTE}"\n'
            "---\nbody\n"
        )
        assert contract["prove_red_kind"] == "coverage"
        assert contract["prove_red_note"] == NOTE
        assert unknown == []


class TestPinExclusion:
    """(e) coverage and pin are mutually exclusive — both expect green on
    the baseline, for different reasons."""

    def test_pin_and_coverage_refused(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            parse_todo_contract(
                '---\n'
                'prove_red_pin: ["tests/test_pin.py"]\n'
                "prove_red_kind: coverage\n"
                'prove_red_note: "why"\n'
                "---\nbody\n"
            )
