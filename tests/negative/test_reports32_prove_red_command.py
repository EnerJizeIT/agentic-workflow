"""REPORTS32: prove-red command mode — diffed test files + the pipe guard.

Red-first: the jql report (a JS project, TODO-0100) ran
``awf_prove_red(command="npx vitest run tests/dashboard-gantt.test.js
tests/loaddata.test.js")`` on a contract with no ``prove_red`` block.
With an empty test list the command mode copied nothing into the
baseline worktree, so the command ran against the BASELINE test files
(129 of them instead of the 130 in the tree) and read as a pass:
``not-red`` with ``copied_files=[]`` — a false "the tests prove
nothing". By hand (worktree + the file copy) the same command gave an
honest red: 5 failed / 158 passed. The same command also ended in
``| tail -40``: even a red runner would have read as a pass, because
the shell rc is the rc of the last command.

After the fix:
- a command mode with an empty test list gathers the tree's test files
  changed since the baseline (tracked changes + untracked, filtered to
  paths that look like tests) and copies them into the worktree;
- an empty gathered set names itself in the verdict message — the
  command ran against the baseline tests — instead of a silent not-red;
- a ``|`` or ``>`` in the command warns that the shell rc is the rc of
  the last command, and a pipe runs under ``bash -o pipefail`` when bash
  is available (without bash, the warning only);
- an explicit ``tests=`` list and the pytest path are untouched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Only ``prove_red`` is imported at module level so the file still
# collects on the pre-fix baseline. On the baseline the new cases then
# fail on a real assertion (the old code returns not-red with nothing
# copied), not a collection error.
from awf.prove_red import prove_red

TODO_ID = "TODO-0001"

#: Stub runner: runs every shell test file found in the tree. Committed
#: to the baseline, so it exists in the worktree; the tree's ``tests/``
#: does not exist on the baseline.
RUNNER_SH = """\
#!/bin/sh
rc=0
for t in tests/*.test.sh; do
  [ -e "$t" ] || continue
  sh "$t" || rc=1
done
exit "$rc"
"""

#: The regression artifact: asserts the marker the fix adds.
NEW_TEST_SH = """\
#!/bin/sh
grep -q "gantt-v2" src/version.txt
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def _new_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    (repo / ".agentic" / "context").mkdir(parents=True)
    (repo / ".agentic" / "inbox").mkdir(parents=True)
    return repo


def _write_baseline(repo: Path) -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (repo / ".agentic" / "context" / f"BASELINE-{TODO_ID}.sha").write_text(
        sha + "\n", encoding="utf-8"
    )
    return sha


def _write_todo(repo: Path, contract_yaml: str) -> None:
    (repo / ".agentic" / "inbox" / f"{TODO_ID}.md").write_text(
        f"---\n{contract_yaml}---\n# Task\n", encoding="utf-8"
    )


def _js_repo(tmp_path: Path, name: str) -> Path:
    """A js-style project: the committed stub runner + a v1 marker."""
    repo = _new_repo(tmp_path, name)
    (repo / "package.json").write_text(
        json.dumps({"name": "jsproj", "scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )
    runner = repo / "runner.sh"
    runner.write_text(RUNNER_SH, encoding="utf-8")
    runner.chmod(0o755)
    (repo / "src").mkdir()
    (repo / "src" / "version.txt").write_text("v1\n", encoding="utf-8")
    _commit(repo, "js baseline")
    _write_baseline(repo)
    return repo


def _add_new_test(repo: Path) -> None:
    """The regression artifact in the current tree: a new (untracked)
    test file + the fix (a tracked change)."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "dashboard-gantt.test.sh").write_text(
        NEW_TEST_SH, encoding="utf-8"
    )
    (repo / "src" / "version.txt").write_text("v2 gantt-v2\n", encoding="utf-8")


# ─── (a) command mode runs against the current tree's test files ────────


class TestDiffedTestFilesCopied:
    def test_new_test_file_copied_and_red_ok(self, tmp_path: Path) -> None:
        repo = _js_repo(tmp_path, "copyrepo")
        _add_new_test(repo)

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt", command="sh runner.sh"
        )

        assert result.verdict == "red-ok"
        assert result.exit_code == 0
        assert result.copied_files == ["tests/dashboard-gantt.test.sh"]
        assert result.tests == ["tests/dashboard-gantt.test.sh"]
        # the set was not empty — no "baseline tests" note
        assert "ran against the baseline tests" not in result.message

    def test_modified_test_file_copied_not_baseline_version(
        self, tmp_path: Path
    ) -> None:
        # a test file that EXISTS on the baseline and is modified since
        # it is in the diff too: the worktree must get the new version
        repo = _new_repo(tmp_path, "modrepo")
        (repo / "src").mkdir()
        (repo / "src" / "version.txt").write_text("v1\n", encoding="utf-8")
        runner = repo / "runner.sh"
        runner.write_text(RUNNER_SH, encoding="utf-8")
        runner.chmod(0o755)
        (repo / "tests").mkdir()
        (repo / "tests" / "old.test.sh").write_text(
            '#!/bin/sh\ngrep -q "v1" src/version.txt\n', encoding="utf-8"
        )
        _commit(repo, "baseline with old test")
        _write_baseline(repo)
        (repo / "src" / "version.txt").write_text("v2 gantt-v2\n", encoding="utf-8")
        (repo / "tests" / "old.test.sh").write_text(
            '#!/bin/sh\ngrep -q "v2" src/version.txt\n', encoding="utf-8"
        )

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt", command="sh runner.sh"
        )

        assert result.verdict == "red-ok"
        assert result.copied_files == ["tests/old.test.sh"]


# ─── (b) an empty gathered set says so in the verdict ───────────────────


class TestEmptyGatheredSet:
    def test_empty_set_named_in_message(self, tmp_path: Path) -> None:
        repo = _js_repo(tmp_path, "emptyrepo")
        # only a non-test tracked change — nothing test-like in the diff
        (repo / "src" / "version.txt").write_text("v2\n", encoding="utf-8")
        _write_todo(repo, 'prove_red_cmd: "sh runner.sh"\n')

        result = prove_red(repo, TODO_ID, tmp_base=tmp_path / "wt")

        assert result.verdict == "not-red"
        assert result.copied_files == []
        assert "ran against the baseline tests" in result.message
        assert "no test files" in result.message


# ─── (c) the pipe guard ─────────────────────────────────────────────────


class TestPipeGuard:
    def test_piped_failing_runner_reads_as_red(self, tmp_path: Path) -> None:
        repo = _js_repo(tmp_path, "piperepo")
        _add_new_test(repo)

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt",
            command="sh runner.sh | tail -40",
        )

        # without pipefail the rc would be tail's (0): a false not-red
        assert result.verdict == "red-ok"
        assert result.copied_files == ["tests/dashboard-gantt.test.sh"]
        assert any(
            "return code is masked by the pipe" in w for w in result.warnings
        )

    def test_redirect_warns_too(self, tmp_path: Path) -> None:
        repo = _js_repo(tmp_path, "redirepo")
        _add_new_test(repo)

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt",
            command="sh runner.sh > /dev/null",
        )

        # the redirect does not mask the rc: still a red, but warned
        assert result.verdict == "red-ok"
        assert any(
            "return code is masked by the pipe" in w for w in result.warnings
        )

    def test_pipe_without_bash_warns_only(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: None)
        repo = _js_repo(tmp_path, "noshrepo")
        _add_new_test(repo)

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt",
            command="sh runner.sh | tail -40",
        )

        # no wrap possible: the masked rc (tail's 0) reads as a pass —
        # the warning is the only honest thing left
        assert result.verdict == "not-red"
        assert any("bash not found" in w for w in result.warnings)


# ─── (d) explicit tests= and the pytest path are untouched ──────────────


class TestExplicitAndPytestUntouched:
    def test_explicit_tests_still_copied(self, tmp_path: Path) -> None:
        repo = _js_repo(tmp_path, "explicitrepo")
        _add_new_test(repo)

        result = prove_red(
            repo, TODO_ID, tmp_base=tmp_path / "wt",
            tests=["tests/dashboard-gantt.test.sh"], command="sh runner.sh",
        )

        assert result.verdict == "red-ok"
        assert result.copied_files == ["tests/dashboard-gantt.test.sh"]

    def test_pytest_path_still_runs_pytest(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "pytestrepo")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "pytestproj"\n', encoding="utf-8"
        )
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        _commit(repo, "buggy add")
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_todo(repo, 'prove_red: ["tests/test_calc.py"]\n')
        _write_baseline(repo)

        result = prove_red(repo, TODO_ID, tmp_base=tmp_path / "wt")

        assert result.verdict == "red-ok"
        assert result.copied_files == ["tests/test_calc.py"]
        assert "AssertionError" in result.baseline_output


# ─── the test-likeness filter ───────────────────────────────────────────


class TestLooksLikeTestFile:
    def test_filter(self, tmp_path: Path) -> None:
        from awf.prove_red import _looks_like_test_file

        assert _looks_like_test_file("tests/a.test.sh")
        assert _looks_like_test_file("deep/nested/tests/x.js")
        assert _looks_like_test_file("src/test_b.py")
        assert _looks_like_test_file("c/spec/c.spec.ts")
        assert _looks_like_test_file("Tests/UPPER.Spec.js")
        assert not _looks_like_test_file("src/version.txt")
        assert not _looks_like_test_file("docs/notes.md")
