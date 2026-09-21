"""U11 Part B (B6): awf mutations — mutation smoke as a supervisor tool.

Parser (empty/comments/malformed/stale), refusal on a dirty tree, and the
killed/survived report with an injected runner. The real runner and a full
mutation run stay out of unit tests (they are slow by design).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from awf import mutations as mut

LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="process groups are Linux-only")


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _committed_repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    (repo / "target.py").write_text("x = 1\n")
    for name, content in (extra or {}).items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


class TestParse:
    def test_parses_valid_lines(self) -> None:
        text = (
            "# comment\n"
            "\n"
            "a.py @@ if ok: @@ if not ok: @@ pytest tests/a.py -q\n"
        )
        ms = mut.parse_mutation_lines(text)
        assert len(ms) == 1
        m = ms[0]
        assert (m.file, m.find, m.repl, m.cmd) == (
            "a.py",
            "if ok:",
            "if not ok:",
            "pytest tests/a.py -q",
        )
        assert m.lineno == 3

    def test_cmd_may_contain_separator(self) -> None:
        text = "a.py @@ one @@ two @@ pytest -q @@ extra\n"
        ms = mut.parse_mutation_lines(text)
        assert ms[0].cmd == "pytest -q @@ extra"

    def test_empty_repl_is_deletion_mutation(self) -> None:
        text = "a.py @@ drop me @@  @@ pytest -q\n"
        ms = mut.parse_mutation_lines(text)
        assert ms[0].repl == ""

    def test_malformed_line_names_lineno(self) -> None:
        text = "a.py @@ one @@ pytest -q\n"
        with pytest.raises(mut.MutationConfigError, match="line 1"):
            mut.parse_mutation_lines(text)

    def test_empty_file_is_config_error(self, tmp_path: Path) -> None:
        f = tmp_path / "m.txt"
        f.write_text("# only comments\n\n")
        with pytest.raises(mut.MutationConfigError, match="no mutations"):
            mut.load_mutations(f)

    def test_missing_file_is_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(mut.MutationConfigError, match="no such file"):
            mut.load_mutations(tmp_path / "absent.txt")


class TestRefusal:
    def test_not_a_git_repo_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        mfile = plain / "m.txt"
        mfile.write_text("a.py @@ one @@ two @@ true\n")
        (plain / "a.py").write_text("one\n")
        with pytest.raises(mut.MutationRefused, match="git repo"):
            mut.run_mutations(plain, mfile)

    def test_dirty_tree_refused(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        (repo / "target.py").write_text("x = 2\n")  # uncommitted change
        mfile = repo / "m.txt"
        mfile.write_text("target.py @@ x = 2 @@ x = 3 @@ true\n")
        with pytest.raises(mut.MutationRefused, match="dirty"):
            mut.run_mutations(repo, mfile)


class TestRunWithMockedRunner:
    def _mfile(self, repo: Path, lines: str) -> Path:
        """The mutations file is part of the repo (like the real
        scripts/mutations.txt) — committed, so the tree stays quiet."""
        mfile = repo / "scripts" / "mutations.txt"
        mfile.parent.mkdir(exist_ok=True)
        mfile.write_text(lines)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "mutations")
        return mfile

    def test_killed_and_survived_report(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        mfile = self._mfile(
            repo,
            "target.py @@ x = 1 @@ x = 2 @@ pytest red\n"
            "target.py @@ x = 1 @@ x = 2 @@ pytest green\n",
        )
        calls: list[str] = []

        def runner(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
            calls.append(cmd)
            return (1, "1 failed") if "red" in cmd else (0, "2 passed")

        report = mut.run_mutations(repo, mfile, runner=runner)

        assert report.total == 2
        assert [o.status for o in report.outcomes] == ["killed", "survived"]
        assert report.killed == 1
        assert report.survived == 1
        assert not report.ok
        assert calls == ["pytest red", "pytest green"]

    def test_all_killed_is_ok(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        mfile = self._mfile(repo, "target.py @@ x = 1 @@ x = 2 @@ pytest\n")

        report = mut.run_mutations(
            repo, mfile, runner=lambda cmd, cwd, timeout: (1, "failed")
        )

        assert report.ok
        assert report.survived == 0

    def test_timeout_is_not_killed(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        mfile = self._mfile(repo, "target.py @@ x = 1 @@ x = 2 @@ pytest\n")

        report = mut.run_mutations(
            repo, mfile, runner=lambda cmd, cwd, timeout: (124, "timeout after 5s")
        )

        assert report.outcomes[0].status == "timeout"
        assert not report.ok

    def test_tree_restored_after_run(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        original = (repo / "target.py").read_text(encoding="utf-8")
        mtime_before = (repo / "target.py").stat().st_mtime_ns
        mfile = self._mfile(repo, "target.py @@ x = 1 @@ x = 2 @@ pytest\n")

        mut.run_mutations(repo, mfile, runner=lambda cmd, cwd, timeout: (1, "red"))

        assert (repo / "target.py").read_text(encoding="utf-8") == original
        assert (repo / "target.py").stat().st_mtime_ns == mtime_before

    def test_tree_restored_when_runner_raises(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        original = (repo / "target.py").read_text(encoding="utf-8")
        mfile = self._mfile(repo, "target.py @@ x = 1 @@ x = 2 @@ pytest\n")

        def boom(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
            raise RuntimeError("runner exploded")

        with pytest.raises(RuntimeError, match="exploded"):
            mut.run_mutations(repo, mfile, runner=boom)

        assert (repo / "target.py").read_text(encoding="utf-8") == original

    def test_stale_mutation_is_config_error(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        mfile = self._mfile(repo, "target.py @@ x = 99 @@ x = 2 @@ pytest\n")
        with pytest.raises(mut.MutationConfigError, match="line 1"):
            mut.run_mutations(repo, mfile, runner=lambda c, w, t: (1, ""))

    def test_missing_target_file_is_config_error(self, tmp_path: Path) -> None:
        repo = _committed_repo(tmp_path)
        mfile = self._mfile(repo, "absent.py @@ one @@ two @@ pytest\n")
        with pytest.raises(mut.MutationConfigError, match="absent.py"):
            mut.run_mutations(repo, mfile, runner=lambda c, w, t: (1, ""))


# ── QA U11: the default runner must kill the whole process group ─────────
# (mirrors the AUD14-03/AUD04-06 pattern of tests/negative/)

_GRANDCHILD = [sys.executable, "-c", "import time; time.sleep(60)"]

_RUNNER_HANG_SPAWNER = f"""\
import subprocess, sys, time
grand = subprocess.Popen({_GRANDCHILD!r})
open(sys.argv[1], "w").write(str(grand.pid))
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


@LINUX_ONLY
class TestDefaultRunnerKillsProcessGroup:
    def test_timeout_kills_grandchildren(self, tmp_path: Path) -> None:
        """A hung test command must not leave the grandchild running.

        Regression (QA, U11): the runner used to call
        ``subprocess.run(shell=True, timeout=...)`` — a timeout killed only
        the shell, orphaning the real test process while the next mutation
        already edited files. The default runner now goes through
        ``awf._proc.run_tree`` (own session + group kill).
        """
        spawner = tmp_path / "spawner.py"
        spawner.write_text(_RUNNER_HANG_SPAWNER)
        pid_file = tmp_path / "grand.pid"

        rc, out = mut._default_runner(
            f"{sys.executable} {spawner} {pid_file}",
            tmp_path,
            timeout=2,
        )

        assert rc == 124, out
        assert "timeout" in out
        grand = int(pid_file.read_text())
        assert _wait_dead(grand), "grandchild survived the runner timeout kill"
