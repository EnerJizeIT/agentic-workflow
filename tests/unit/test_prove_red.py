"""U4 tests for awf.prove_red — on real temporary git repos.

Each scenario builds a tiny repo with a buggy ``calc.py`` as the committed
baseline, then varies the working tree and the test files. The baseline is
deployed into a real ``git worktree`` under the test's tmp_path, so
worktree lifecycle (create/copy/remove/prune) is exercised for real.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api
from awf.cmd_prove_red import run as cmd_run
from awf.prove_red import prove_red


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    """Git repo with a buggy calc.py (committed = the baseline)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / ".agentic" / "context").mkdir(parents=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: buggy add")
    return repo


def _write_baseline(repo: Path, todo_id: str = "TODO-0001") -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (repo / ".agentic" / "context" / f"BASELINE-{todo_id}.sha").write_text(
        sha + "\n"
    )
    return sha


def _add_test(repo: Path, body: str) -> None:
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_calc.py").write_text(body)


RED_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
BUGGY_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == -1\n"


# ─── Verdicts ───────────────────────────────────────────────────────────


class TestVerdicts:
    def test_red_ok_honest_red(self, calc_repo: Path, tmp_path: Path) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        sha = _write_baseline(calc_repo)

        result = prove_red(
            calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "red-ok"
        assert result.exit_code == 0
        assert result.baseline_sha == sha
        assert result.copied_files == ["tests/test_calc.py"]
        assert "AssertionError" in result.baseline_output
        assert "red-ok" in result.message.lower() or "RED-OK" in result.message

    def test_not_red_when_passes_on_baseline(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, BUGGY_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_baseline(calc_repo)

        result = prove_red(
            calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "not-red"
        assert result.exit_code == 1
        assert "PASSED on baseline" in result.message
        # not-red short-circuits: the current tree was never run
        assert result.current_output == ""

    def test_green_after_when_current_tree_still_fails(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        # calc.py is NOT fixed in the working tree
        _write_baseline(calc_repo)

        result = prove_red(
            calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "green-after"
        assert result.exit_code == 1
        assert "do NOT pass" in result.message
        assert "AssertionError" in result.current_output


# ─── broken-runner ──────────────────────────────────────────────────────


class TestBrokenRunner:
    def test_unknown_test_id(self, calc_repo: Path, tmp_path: Path) -> None:
        _add_test(calc_repo, BUGGY_TEST)
        _write_baseline(calc_repo)

        result = prove_red(
            calc_repo, "TODO-0001",
            tests=["tests/test_calc.py::test_missing"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "broken-runner"
        assert result.exit_code == 2
        assert "0 collected tests is not a red result" in result.message

    def test_new_module_collection_error(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        # module-level import of a module that does not exist on the
        # baseline -> collection ImportError, 0 tests executed
        (calc_repo / "tests").mkdir()
        (calc_repo / "tests" / "test_new.py").write_text(
            "from newmod import new\n\n\ndef test_new():\n    assert new() == 42\n"
        )
        (calc_repo / "newmod.py").write_text("def new():\n    return 42\n")
        _write_baseline(calc_repo)

        result = prove_red(
            calc_repo, "TODO-0001", tests=["tests/test_new.py"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "broken-runner"
        assert result.exit_code == 2
        assert "No module named" in result.baseline_output

    def test_symbol_missing_red_is_broken_with_warning(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        # the module exists on the baseline but the symbol does not:
        # the failure is only "no symbol", which is expected for new code
        _git(calc_repo, "add", "calc.py")
        (calc_repo / "newmod.py").write_text("def old():\n    return 1\n")
        _git(calc_repo, "add", "-A")
        _git(calc_repo, "commit", "-qm", "newmod without new()")
        (calc_repo / "newmod.py").write_text(
            "def old():\n    return 1\n\n\ndef new():\n    return 42\n"
        )
        (calc_repo / "tests").mkdir()
        (calc_repo / "tests" / "test_new.py").write_text(
            "def test_new():\n"
            "    from newmod import new\n"
            "    assert new() == 42\n"
        )
        _write_baseline(calc_repo, "TODO-0002")

        result = prove_red(
            calc_repo, "TODO-0002", tests=["tests/test_new.py"],
            tmp_base=tmp_path / "wt",
        )

        assert result.verdict == "broken-runner"
        assert result.exit_code == 2
        assert "missing-symbol" in result.message
        assert any("new code" in w.lower() for w in result.warnings)


# ─── Preconditions ──────────────────────────────────────────────────────


class TestPreconditions:
    def test_missing_baseline_refused(self, calc_repo: Path, tmp_path: Path) -> None:
        _add_test(calc_repo, RED_TEST)
        with pytest.raises(api.AwfApiError, match="baseline not found"):
            prove_red(
                calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
                tmp_base=tmp_path / "wt",
            )

    def test_invalid_baseline_sha_refused(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            "not-a-sha\n"
        )
        with pytest.raises(api.AwfApiError, match="expected a 7-40 char git sha"):
            prove_red(
                calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
                tmp_base=tmp_path / "wt",
            )

    def test_baseline_sha_not_in_repo_refused(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            "f" * 40 + "\n"
        )
        with pytest.raises(api.AwfApiError, match="not a commit"):
            prove_red(
                calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
                tmp_base=tmp_path / "wt",
            )

    def test_test_file_not_found(self, calc_repo: Path, tmp_path: Path) -> None:
        _write_baseline(calc_repo)
        with pytest.raises(api.AwfApiError, match="test file not found"):
            prove_red(
                calc_repo, "TODO-0001", tests=["tests/nope.py"],
                tmp_base=tmp_path / "wt",
            )

    def test_no_tests_and_no_contract(self, calc_repo: Path, tmp_path: Path) -> None:
        _write_baseline(calc_repo)
        with pytest.raises(api.AwfApiError, match="prove_red block"):
            prove_red(calc_repo, "TODO-0001", tmp_base=tmp_path / "wt")

    def test_bad_todo_id(self, calc_repo: Path) -> None:
        with pytest.raises(api.AwfApiError, match="TODO-NNNN"):
            prove_red(calc_repo, "task-1", tests=["x"])


# ─── Contract default + CLI + hygiene ───────────────────────────────────


class TestContractAndCli:
    def test_tests_default_from_todo_contract(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_baseline(calc_repo)
        inbox = calc_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text(
            "---\n"
            'prove_red: ["tests/test_calc.py::test_add"]\n'
            "---\n"
            "# TODO-0001 — task\n"
        )

        result = prove_red(calc_repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "red-ok"
        assert result.exit_code == 0
        assert result.tests == ["tests/test_calc.py::test_add"]

    def test_cli_returns_verdict_exit_code(
        self, calc_repo: Path, tmp_path: Path, capsys
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_baseline(calc_repo)
        args = SimpleNamespace(
            todo_id="TODO-0001", project_dir=str(calc_repo),
            tests=["tests/test_calc.py"], json=False,
        )
        rc = cmd_run(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "verdict: red-ok" in out

    def test_cli_json_output(
        self, calc_repo: Path, tmp_path: Path, capsys
    ) -> None:
        _add_test(calc_repo, BUGGY_TEST)
        _write_baseline(calc_repo)
        args = SimpleNamespace(
            todo_id="TODO-0001", project_dir=str(calc_repo),
            tests=["tests/test_calc.py"], json=True,
        )
        rc = cmd_run(args)
        data = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert data["verdict"] == "not-red"
        for key in (
            "todo_id", "verdict", "exit_code", "baseline_sha", "tests",
            "copied_files", "baseline_output", "message",
        ):
            assert key in data

    def test_cli_missing_baseline_exits_2(
        self, calc_repo: Path, capsys
    ) -> None:
        args = SimpleNamespace(
            todo_id="TODO-0001", project_dir=str(calc_repo),
            tests=["tests/test_calc.py"], json=False,
        )
        rc = cmd_run(args)
        err = capsys.readouterr().err
        assert rc == 2
        assert "baseline not found" in err


class TestCliWiring:
    def test_main_dispatches_prove_red(
        self, calc_repo: Path, tmp_path: Path, capsys
    ) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_baseline(calc_repo)

        from awf import cli

        rc = cli.main([
            "prove-red", "--todo", "TODO-0001",
            "--tests", "tests/test_calc.py",
            "--project-dir", str(calc_repo),
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "verdict: red-ok" in out

    def test_main_requires_todo_flag(self, calc_repo: Path, capsys) -> None:
        from awf import cli

        with pytest.raises(SystemExit):
            cli.main(["prove-red", "--project-dir", str(calc_repo)])


class TestHermeticBootstrap:
    """The baseline run must not see an installed copy of the project.

    Two real leak shapes, reproduced: (1) a sys.path entry providing a
    duplicate of the project's own top-level packages (old-style editable
    install), (2) a meta-path finder installed at startup (PEP 660
    editable install).
    """

    def _build(self, tmp_path: Path) -> tuple[Path, Path]:
        work = tmp_path / "work"
        leak = tmp_path / "leak"
        (work / "tests").mkdir(parents=True)
        leak.mkdir()
        (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (work / "tests" / "test_newmod.py").write_text(
            "from newmod import NEW\n\n\ndef test_new():\n    assert NEW\n"
        )
        (leak / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (leak / "newmod.py").write_text("NEW = True\n")
        return work, leak

    def test_blocks_duplicate_sys_path_provider(self, tmp_path: Path) -> None:
        from awf.prove_red import _BOOTSTRAP_NAME, _BOOTSTRAP_SOURCE

        work, leak = self._build(tmp_path)
        env = {**os.environ, "PYTHONPATH": str(leak)}

        # control: without the scrub the leak provides newmod -> test passes
        plain = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_newmod.py"],
            cwd=work, env=env, capture_output=True, text=True, timeout=120,
        )
        assert plain.returncode == 0, plain.stdout

        # hermetic: the duplicate provider is dropped -> newmod unimportable
        (work / _BOOTSTRAP_NAME).write_text(_BOOTSTRAP_SOURCE, encoding="utf-8")
        boot = subprocess.run(
            [sys.executable, str(work / _BOOTSTRAP_NAME), "-q",
             "-p", "no:cacheprovider", "tests/test_newmod.py"],
            cwd=work, env=env, capture_output=True, text=True, timeout=120,
        )
        assert boot.returncode != 0
        assert "No module named 'newmod'" in boot.stdout

    def test_blocks_editable_meta_path_finder(self, tmp_path: Path) -> None:
        from awf.prove_red import _BOOTSTRAP_NAME, _BOOTSTRAP_SOURCE

        work, leak = self._build(tmp_path)
        site_dir = tmp_path / "fake_site"
        site_dir.mkdir()
        (site_dir / "editable_fake_finder.py").write_text(
            "import sys\n"
            "from importlib.machinery import ModuleSpec\n"
            "from importlib.util import spec_from_file_location\n"
            "\n"
            "TARGET = None\n"
            "\n"
            "\n"
            "class _FakeEditableFinder:\n"
            "    @classmethod\n"
            "    def find_spec(cls, fullname, path=None, target=None):\n"
            "        if fullname == 'newmod' and TARGET:\n"
            "            return spec_from_file_location(fullname, TARGET)\n"
            "        return None\n"
            "\n"
            "\n"
            "def install():\n"
            "    sys.meta_path.append(_FakeEditableFinder)\n"
        )
        (site_dir / "sitecustomize.py").write_text(
            f"import editable_fake_finder\n"
            f"editable_fake_finder.TARGET = r'{leak / 'newmod.py'}'\n"
            f"editable_fake_finder.install()\n"
        )
        env = {**os.environ, "PYTHONPATH": str(site_dir)}

        # control: the finder leaks newmod into the "baseline" -> test passes
        plain = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_newmod.py"],
            cwd=work, env=env, capture_output=True, text=True, timeout=120,
        )
        assert plain.returncode == 0, plain.stdout

        (work / _BOOTSTRAP_NAME).write_text(_BOOTSTRAP_SOURCE, encoding="utf-8")
        boot = subprocess.run(
            [sys.executable, str(work / _BOOTSTRAP_NAME), "-q",
             "-p", "no:cacheprovider", "tests/test_newmod.py"],
            cwd=work, env=env, capture_output=True, text=True, timeout=120,
        )
        assert boot.returncode != 0
        assert "No module named 'newmod'" in boot.stdout


class TestWorktreeHygiene:
    def test_no_worktree_leftovers(self, calc_repo: Path, tmp_path: Path) -> None:
        _add_test(calc_repo, RED_TEST)
        (calc_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_baseline(calc_repo)
        base = tmp_path / "wt"

        result = prove_red(
            calc_repo, "TODO-0001", tests=["tests/test_calc.py"],
            tmp_base=base,
        )
        assert result.verdict == "red-ok"

        listed = subprocess.run(
            ["git", "worktree", "list"], cwd=calc_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        assert len(listed) == 1  # only the main worktree
        assert not any(Path(line.split()[0]) == base for line in listed)
        # no leftover directories in the tmp base
        leftovers = [p for p in base.iterdir() if p.name.startswith("prove-red")]
        assert leftovers == []

    def test_worktree_removed_even_on_broken_runner(
        self, calc_repo: Path, tmp_path: Path
    ) -> None:
        _add_test(calc_repo, BUGGY_TEST)
        _write_baseline(calc_repo)
        base = tmp_path / "wt"

        result = prove_red(
            calc_repo, "TODO-0001",
            tests=["tests/test_calc.py::test_missing"], tmp_base=base,
        )
        assert result.verdict == "broken-runner"
        assert not any(p.name.startswith("prove-red") for p in base.iterdir())
