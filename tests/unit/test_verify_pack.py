"""U5 tests for awf.verify_pack — on real temporary projects.

Each scenario builds a tiny git repo with a committed baseline and a fast
contract gate (docs/contracts/). Gates, diff, contract commands and lint
run for real; only prove-red and (in the exit-2 case) ruff availability
are monkeypatched.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from awf import verify_pack as vp
from awf.cmd_verify_pack import run as cmd_run
from awf.todos import archive_todo

TODOS = "TODO-0001"
OK_GATE = 'python3 -c "import calc; assert calc.add(1, 2) == 3"'
BAD_GATE = 'python3 -c "import calc; assert calc.add(1, 2) == 4"'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _proj(tmp_path: Path, contract: str | None = None, gate: str = OK_GATE,
          name: str = "proj") -> Path:
    """Git repo: committed calc.py baseline + a contract gate in docs/contracts."""
    repo = tmp_path / name
    (repo / "docs" / "contracts").mkdir(parents=True)
    (repo / ".agentic" / "inbox").mkdir(parents=True)
    (repo / ".agentic" / "outbox").mkdir(parents=True)
    (repo / ".agentic" / "context").mkdir(parents=True)
    # The real gate script (the temp repo has the precondition, docs/contracts)
    (repo / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "check-contracts.sh",
                repo / "scripts" / "check-contracts.sh")
    (repo / ".gitignore").write_text(".agentic/\n__pycache__/\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "docs" / "contracts" / "calc.md").write_text(
        f"# Contract: calc\n\nOwns: calc.add\nPath: `calc.py`\nNever: no eval\nGate: {gate}\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    (repo / ".agentic" / "context" / f"BASELINE-{TODOS}.sha").write_text(_sha(repo) + "\n")
    (repo / ".agentic" / "inbox" / f"{TODOS}.md").write_text(contract or "# TODO\nwork\n")
    return repo


def _run(repo: Path, **kw) -> vp.VerifyPackResult:
    return vp.verify_pack(repo, TODOS, **kw)


def _report(repo: Path) -> str:
    return (repo / ".agentic" / "context" / f"GATES-{TODOS}.md").read_text(encoding="utf-8")


# ─── verdicts ────────────────────────────────────────────────────────────


class TestVerdicts:
    def test_all_ok_exit_0_denominators(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n")
        (repo / "extra.py").write_text("x = 1\n")

        res = _run(repo)
        assert res.exit_code == 0
        assert res.verdict == "ok"
        assert res.sections["gates"] == "pass"
        assert res.sections["diff"] == "pass"
        assert res.sections["lint"] == "pass"

        report = _report(repo)
        assert "1 contracts" in report and "1 path tokens" in report  # gate denominator
        assert "1 new untracked" in report  # diff denominator
        assert "full suite" in report  # full pytest suite is QA's job
        assert '"verdict": "ok"' in report  # machine JSON block

    def test_broken_gate_exit_1(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, gate=BAD_GATE)
        res = _run(repo)
        assert res.exit_code == 1
        assert res.verdict == "failed"
        assert res.sections["gates"] == "fail"
        assert "FAILED" in _report(repo)
        assert res.sections["safety"] == "skipped"  # not an awf repo

    def test_nothing_measured_exit_2(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "bare"
        (repo / ".agentic" / "inbox").mkdir(parents=True)
        (repo / ".agentic" / "outbox").mkdir(parents=True)
        (repo / ".agentic" / "context").mkdir(parents=True)
        (repo / ".agentic" / "inbox" / f"{TODOS}.md").write_text("# TODO\n")
        monkeypatch.setattr(vp, "_run_lint", lambda cwd, timeout: (None, ""))

        res = _run(repo)
        assert res.exit_code == 2
        assert res.verdict == "nothing-measured"
        assert res.measured == 0
        assert "nothing-measured" in _report(repo)


# ─── contract verify: commands ───────────────────────────────────────────


class TestContractTests:
    def test_failing_command(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, contract='---\nverify:\n  - "false"\n---\n# TODO\n')
        res = _run(repo)
        assert res.exit_code == 1
        assert res.sections["contract_tests"] == "fail"
        assert "FAIL (rc=1)" in _report(repo)

    def test_timeout_marked(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, contract='---\nverify:\n  - "sleep 5"\n---\n# TODO\n')
        res = _run(repo, cmd_timeout=1)
        assert res.sections["contract_tests"] == "fail"
        assert "timeout after 1s" in _report(repo)

    def test_passing_command(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, contract='---\nverify:\n  - "true"\n---\n# TODO\n')
        res = _run(repo)
        assert res.sections["contract_tests"] == "pass"
        assert res.exit_code == 0


# ─── prove-red section ───────────────────────────────────────────────────


class TestProveRed:
    def test_spoofed_verdict_visible(self, tmp_path: Path, monkeypatch) -> None:
        repo = _proj(tmp_path, contract='---\nprove_red:\n  - "tests/test_calc.py::test_add"\n---\n# TODO\n')
        fake = SimpleNamespace(
            todo_id=TODOS, verdict="not-red", exit_code=1,
            baseline_sha="a" * 40, tests=["tests/test_calc.py::test_add"],
            copied_files=[], baseline_output="", current_output="",
            message="tests passed on baseline — they prove nothing",
            warnings=["w1"],
        )
        monkeypatch.setattr(vp, "prove_red", lambda *a, **kw: fake)

        res = _run(repo)
        assert res.sections["prove_red"] == "fail"
        assert "not-red" in _report(repo)
        assert "w1" in _report(repo)

    def test_red_ok_passes(self, tmp_path: Path, monkeypatch) -> None:
        repo = _proj(tmp_path, contract='---\nprove_red:\n  - "tests/test_calc.py::test_add"\n---\n# TODO\n')
        fake = SimpleNamespace(
            todo_id=TODOS, verdict="red-ok", exit_code=0,
            baseline_sha="a" * 40, tests=[], copied_files=[],
            baseline_output="", current_output="", message="ok", warnings=[],
        )
        monkeypatch.setattr(vp, "prove_red", lambda *a, **kw: fake)

        res = _run(repo)
        assert res.sections["prove_red"] == "pass"
        assert res.exit_code == 0


# ─── DONE.json facts ─────────────────────────────────────────────────────


class TestDoneJson:
    def test_valid_facts(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        (repo / ".agentic" / "outbox" / f"DONE-{TODOS}.json").write_text(
            '{"files_changed": ["calc.py"], '
            '"tests_run": [{"cmd": "pytest", "result": "1 passed"}], "notes": "ok"}'
        )
        res = _run(repo)
        assert res.sections["done_json"] == "pass"
        report = _report(repo)
        assert "executor data" in report and "unverified" in report
        assert "calc.py" in report and "1 passed" in report

    def test_broken_json_not_crash(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        (repo / ".agentic" / "outbox" / f"DONE-{TODOS}.json").write_text("{oops")
        res = _run(repo)
        assert res.sections["done_json"] == "skipped"
        assert "broken" in _report(repo)
        assert res.exit_code in (0, 1)


# ─── diff minimality ─────────────────────────────────────────────────────


class TestDiff:
    def test_no_baseline_skipped(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        (repo / ".agentic" / "context" / f"BASELINE-{TODOS}.sha").unlink()
        res = _run(repo)
        assert res.sections["diff"] == "skipped"

    def test_undeclared_file_fails(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, contract='---\nfiles:\n  - calc.py\n---\n# TODO\n')
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n")
        (repo / "stray.py").write_text("y = 2\n")
        res = _run(repo)
        assert res.sections["diff"] == "fail"
        report = _report(repo)
        assert "UNDECLARED" in report and "stray.py" in report

    def test_all_declared_passes(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path, contract='---\nfiles:\n  - calc.py\n  - extra.py\n---\n# TODO\n')
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n")
        (repo / "extra.py").write_text("x = 1\n")
        res = _run(repo)
        assert res.sections["diff"] == "pass"
        assert "cross-check vs contract" in _report(repo)


# ─── CLI ─────────────────────────────────────────────────────────────────


class TestCli:
    def test_json_output_and_exit_codes(self, tmp_path: Path, capsys) -> None:
        repo = _proj(tmp_path)
        rc = cmd_run(SimpleNamespace(todo_id=TODOS, project_dir=str(repo), json=True))
        assert rc == 0
        assert '"verdict": "ok"' in capsys.readouterr().out

        bad = _proj(tmp_path, gate=BAD_GATE, name="bad")
        rc = cmd_run(SimpleNamespace(todo_id=TODOS, project_dir=str(bad), json=True))
        assert rc == 1
        assert '"verdict": "failed"' in capsys.readouterr().out

    def test_no_agentic_is_error(self, tmp_path: Path, capsys) -> None:
        rc = cmd_run(SimpleNamespace(todo_id=TODOS, project_dir=str(tmp_path), json=True))
        assert rc == 2
        assert "not an awf project" in capsys.readouterr().err


# ─── git timeout degradation (FU-21 D3) ─────────────────────────────────


class TestGitTimeoutDegradation:
    """A hung git call must degrade its section, not kill the pack."""

    def test_hung_git_degrades_diff_section(self, tmp_path: Path, monkeypatch) -> None:
        repo = _proj(tmp_path, name="githang")

        def _hang(*args, **kw):
            raise subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=30)

        monkeypatch.setattr(vp.subprocess, "run", _hang)

        # Before the fix this raised TimeoutExpired out of _git_lines.
        section = vp._diff_section(repo, TODOS, None)
        assert section.status == "fail"
        assert any("timed out" in ln for ln in section.lines)


# ─── archiving (U3 DONE.json lifecycle) ──────────────────────────────────


class TestArchiveDoneJson:
    def test_done_json_moves_to_done_outbox_clean(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        inbox = project / ".agentic" / "inbox"
        outbox = project / ".agentic" / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)
        (inbox / f"{TODOS}.md").write_text("# TODO")
        (outbox / f"DONE-{TODOS}.ready").write_text("")
        (outbox / f"DONE-{TODOS}.json").write_text('{"files_changed": ["a.py"]}')

        dest = archive_todo(project, TODOS)
        assert dest is not None
        assert (dest / "DONE.json").is_file()
        assert not (outbox / f"DONE-{TODOS}.json").exists()
        assert list(outbox.iterdir()) == []


# ─── reject-leak warning (RUN5 #1, TODO-0052) ────────────────────────────


class TestRejectLeakSection:
    """Part B: the pack must loudly warn about rejected-attempt files that
    would be silently excluded from the commit."""

    RETRY = "TODO-0002"

    def _with_leak(self, repo: Path, baseline_untracked: str | None = "src/a.py\n") -> None:
        (repo / ".agentic" / "context" / f"REJECT-{TODOS}.files").write_text("src/a.py\n")
        if baseline_untracked is not None:
            (repo / ".agentic" / "context" / f"BASELINE-{self.RETRY}.untracked").write_text(
                baseline_untracked
            )
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "a.py").write_text("x\n")

    def test_warns_on_orphaned_files(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_leak(repo)

        section = vp._reject_leak_section(repo, self.RETRY)
        assert section.status == "pass"  # a warning, not a verdict failure
        assert section.measured is True
        assert any("WARNING" in ln for ln in section.lines)
        assert any("src/a.py" in ln for ln in section.lines)
        assert "carry_over_from" in " ".join(section.lines)

    def test_skipped_without_reject_files(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        section = vp._reject_leak_section(repo, self.RETRY)
        assert section.status == "skipped"
        assert section.measured is False

    def test_no_warning_after_carry_over(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_leak(repo, baseline_untracked="stale.txt\n")  # a.py excluded

        section = vp._reject_leak_section(repo, self.RETRY)
        assert section.status == "pass"
        assert section.measured is True
        assert not any("WARNING" in ln for ln in section.lines)

    def test_no_warning_without_baseline_untracked(self, tmp_path: Path) -> None:
        """No BASELINE-<todo>.untracked → the gate would include all
        untracked → nothing is lost → no warning."""
        repo = _proj(tmp_path)
        self._with_leak(repo, baseline_untracked=None)
        section = vp._reject_leak_section(repo, self.RETRY)
        assert section.status == "pass"
        assert not any("WARNING" in ln for ln in section.lines)

    def test_full_pack_carries_the_warning(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_leak(repo)

        res = vp.verify_pack(repo, self.RETRY)
        assert res.sections["reject_leak"] == "pass"
        report = (repo / ".agentic" / "context" / f"GATES-{self.RETRY}.md").read_text(
            encoding="utf-8"
        )
        assert "reject_leak" in report
        assert "WARNING" in report and "src/a.py" in report
        assert "carry_over_from" in report


# ─── untracked-excluded section (RUN10 #4, TODO-0074) ────────────────────


class TestUntrackedExcludedSection:
    """The pack lists the pre-existing untracked files the commit gate
    excludes from the commit — informational, never a verdict failure."""

    def _with_pre_existing(self, repo: Path, listing: str = "x.md\nstale.txt\n") -> None:
        (repo / ".agentic" / "context" / f"BASELINE-{TODOS}.untracked").write_text(listing)
        (repo / "x.md").write_text("pre-existing\n")
        (repo / "stale.txt").write_text("pre-existing\n")

    def test_lists_excluded_files(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_pre_existing(repo)
        section = vp._untracked_excluded_section(repo, TODOS)
        assert section.status == "pass"
        assert section.measured is True
        assert any("x.md" in ln for ln in section.lines)
        assert any("stale.txt" in ln for ln in section.lines)
        assert not any("WARNING" in ln for ln in section.lines), "informational, not a warning"

    def test_skipped_without_baseline_snapshot(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        section = vp._untracked_excluded_section(repo, TODOS)
        assert section.status == "skipped"
        assert section.measured is False

    def test_empty_snapshot_passes(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_pre_existing(repo, listing="")
        section = vp._untracked_excluded_section(repo, TODOS)
        assert section.status == "pass"
        assert section.measured is True
        assert not any("x.md" in ln for ln in section.lines)

    def test_full_pack_carries_the_section(self, tmp_path: Path) -> None:
        repo = _proj(tmp_path)
        self._with_pre_existing(repo, listing="x.md\n")
        res = vp.verify_pack(repo, TODOS)
        assert res.sections["untracked_excluded"] == "pass"
        assert "untracked_excluded" in _report(repo) and "x.md" in _report(repo)
