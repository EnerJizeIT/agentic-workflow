"""REPORTS26-B6: honest verdict for non-pytest projects + prove_red_cmd.

Red-first: before the fix, a JS project (vitest) made ``prove_red`` return
``broken-runner`` — it reads as "the tests are broken", while the real
cause was an unrecognized runner (the tool only knows how to run pytest).
And there was no way to declare the project's actual check command.

After the fix:
- the runner is detected from the top-level manifest (``package.json`` →
  js, ``go.mod`` → go, ``Cargo.toml`` → cargo, ``pyproject.toml`` /
  ``setup.py`` → pytest, no manifest → the pytest default stays);
- a non-pytest project WITHOUT a custom command gets the verdict
  ``runner-unsupported`` (exit 2, like broken-runner, a separate line) and
  a message naming the runner, the manifest and how to override;
- ``prove_red_cmd`` in the TODO contract (or the ``command`` parameter)
  runs the project's own check with shell semantics in the baseline
  worktree and in the current tree; verdicts map by exit code: 0 on the
  baseline = not-red, red on the baseline + 0 now = red-ok, red in both =
  green-after; the ``prove_red:`` files are still copied into the worktree;
- a custom command that did not run on the baseline — a timeout, or shell
  rc 126/127 (not executable / not found) — is ``broken-runner``, never a
  red: the old code counted such a killed/absent command as a red
  (red-ok when the current tree passed);
- python projects (pyproject.toml, or no manifest) keep the pytest path
  exactly as before.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Only ``prove_red`` is imported at module level so the file still collects
# on the pre-fix baseline (where ``_detect_runner`` does not exist yet). On
# the baseline the ``TestRunnerUnsupported`` cases then fail on a real
# assertion (the old code returns ``broken-runner``), not a collection error.
from awf.prove_red import prove_red


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


def _write_baseline(repo: Path, todo_id: str = "TODO-0001") -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (repo / ".agentic" / "context" / f"BASELINE-{todo_id}.sha").write_text(
        sha + "\n", encoding="utf-8"
    )
    return sha


def _write_todo(repo: Path, contract_yaml: str, todo_id: str = "TODO-0001") -> None:
    (repo / ".agentic" / "inbox" / f"{todo_id}.md").write_text(
        f"---\n{contract_yaml}---\n# Task\n", encoding="utf-8"
    )


VITEST_PKG = json.dumps(
    {
        "name": "jsproj",
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^2.0.0"},
    },
    indent=2,
)


# ─── (a) non-pytest project without a command: runner-unsupported ───────


class TestRunnerUnsupported:
    def test_js_project_is_runner_unsupported(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "jsrepo")
        (repo / "package.json").write_text(VITEST_PKG, encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "sample.test.ts").write_text(
            "import {expect, test} from 'vitest'\n\ntest('ok', () => expect(1).toBe(1))\n",
            encoding="utf-8",
        )
        _commit(repo, "js baseline")
        _write_todo(repo, 'prove_red: ["tests/sample.test.ts"]\n')
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "runner-unsupported"
        assert result.verdict != "broken-runner"
        assert result.exit_code == 2
        # the message names the runner, the manifest and the override
        assert "vitest" in result.message
        assert "manifest: package.json" in result.message
        assert "prove_red_cmd" in result.message
        # nothing was run: no files copied, no worktree left behind
        assert result.copied_files == []
        assert result.baseline_output == ""
        leftovers = [
            p for p in (tmp_path / "wt").iterdir() if p.name.startswith("prove-red")
        ] if (tmp_path / "wt").is_dir() else []
        assert leftovers == []

    def test_go_project_is_runner_unsupported(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "gorepo")
        (repo / "go.mod").write_text("module example.com/proj\n", encoding="utf-8")
        (repo / "internal").mkdir()
        (repo / "internal" / "x_test.go").write_text(
            "package internal\n", encoding="utf-8"
        )
        _commit(repo, "go baseline")
        _write_todo(repo, 'prove_red: ["internal/x_test.go"]\n')
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "runner-unsupported"
        assert result.exit_code == 2
        assert "manifest: go.mod" in result.message
        assert "prove_red_cmd" in result.message

    def test_cargo_project_is_runner_unsupported(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "crrepo")
        (repo / "Cargo.toml").write_text(
            '[package]\nname = "crproj"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        _commit(repo, "cargo baseline")
        _write_todo(repo, 'prove_red: ["src/lib.rs"]\n')
        (repo / "src").mkdir()
        (repo / "src" / "lib.rs").write_text("// lib\n", encoding="utf-8")
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "runner-unsupported"
        assert result.exit_code == 2
        assert "manifest: Cargo.toml" in result.message

    def test_js_without_known_runner_reports_category(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "jsplain")
        (repo / "package.json").write_text(
            json.dumps({"name": "plain", "scripts": {"test": "custom-check"}}),
            encoding="utf-8",
        )
        _commit(repo, "plain js baseline")
        _write_todo(repo, 'prove_red: ["tests/a.js"]\n')
        (repo / "tests").mkdir()
        (repo / "tests" / "a.js").write_text("module.exports = {}\n", encoding="utf-8")
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "runner-unsupported"
        assert "js project" in result.message
        assert "vitest" not in result.message

    def test_package_json_wins_over_pyproject(self, tmp_path: Path) -> None:
        # the documented detection order: package.json is checked first
        repo = _new_repo(tmp_path, "poly")
        (repo / "package.json").write_text(VITEST_PKG, encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            "[project]\nname = \"poly\"\n", encoding="utf-8"
        )
        _commit(repo, "poly baseline")
        _write_todo(repo, 'prove_red: ["tests/b.js"]\n')
        (repo / "tests").mkdir()
        (repo / "tests" / "b.js").write_text("module.exports = {}\n", encoding="utf-8")
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "runner-unsupported"
        assert "manifest: package.json" in result.message


# ─── detection table (manifest → runner) ─────────────────────────────────


class TestDetectRunner:
    def test_detection_by_manifest(self, tmp_path: Path) -> None:
        # new symbol — imported lazily so the module collects on the baseline
        from awf.prove_red import _detect_runner

        cases = [
            (["package.json"], "js"),
            (["go.mod"], "go"),
            (["Cargo.toml"], "cargo"),
            (["pyproject.toml"], "pytest"),
            (["setup.py"], "pytest"),
            ([], "unknown"),
        ]
        for i, (manifests, expected) in enumerate(cases):
            d = tmp_path / f"d{i}"
            d.mkdir()
            for m in manifests:
                (d / m).write_text("{}", encoding="utf-8")
            runner, manifest = _detect_runner(d)
            assert runner == expected, (manifests, runner)
            if expected != "unknown":
                assert manifest == manifests[0]

    def test_js_runner_name_from_scripts_and_deps(self, tmp_path: Path) -> None:
        from awf.prove_red import _detect_runner

        d = tmp_path / "js"
        d.mkdir()
        (d / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {"test": "vitest run"},
                    "devDependencies": {"jest": "^29.0.0"},
                }
            ),
            encoding="utf-8",
        )
        # both runners named — the name is read from the manifest
        assert _detect_runner(d) == ("js (vitest/jest)", "package.json")


# ─── (b) prove_red_cmd: marker file via the contract ─────────────────────


def _marker_repo(tmp_path: Path, name: str, marker_committed: bool) -> Path:
    repo = _new_repo(tmp_path, name)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "markerproj"\n', encoding="utf-8"
    )
    if marker_committed:
        (repo / "fix.marker").write_text("marker\n", encoding="utf-8")
    _commit(repo, "marker baseline" if marker_committed else "no marker baseline")
    _write_todo(repo, 'prove_red_cmd: "test -f fix.marker"\n')
    _write_baseline(repo)
    return repo


class TestProveRedCmd:
    def test_red_ok_when_marker_appears_in_current_tree(
        self, tmp_path: Path
    ) -> None:
        repo = _marker_repo(tmp_path, "mkredok", marker_committed=False)
        (repo / "fix.marker").write_text("marker\n", encoding="utf-8")

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "red-ok"
        assert result.exit_code == 0
        assert result.copied_files == []
        assert "RED-OK" in result.message
        assert result.tests == []

    def test_not_red_when_marker_already_on_baseline(self, tmp_path: Path) -> None:
        repo = _marker_repo(tmp_path, "mknotred", marker_committed=True)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "not-red"
        assert result.exit_code == 1
        assert "PASSED on baseline" in result.message

    def test_green_after_when_command_still_red(self, tmp_path: Path) -> None:
        repo = _marker_repo(tmp_path, "mkgreen", marker_committed=False)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "green-after"
        assert result.exit_code == 1
        assert "does NOT pass" in result.message

    def test_command_parameter_beats_contract_key(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "prio")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "prio"\n', encoding="utf-8"
        )
        _commit(repo, "prio baseline")
        _write_todo(repo, 'prove_red_cmd: "false"\n')
        _write_baseline(repo)

        # parameter "true" passes on the baseline → not-red (param wins)
        via_param = prove_red(
            repo, "TODO-0001", tmp_base=tmp_path / "wt", command="true"
        )
        assert via_param.verdict == "not-red"
        assert via_param.exit_code == 1

        # contract "false" is red on the baseline AND in the current tree
        via_contract = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")
        assert via_contract.verdict == "green-after"
        assert via_contract.exit_code == 1

    def test_command_without_prove_red_list_is_accepted(
        self, tmp_path: Path
    ) -> None:
        # a prove_red_cmd-only contract must not trigger the "no tests"
        # error — the command itself is the check
        repo = _marker_repo(tmp_path, "mkcmdonly", marker_committed=False)
        (repo / "fix.marker").write_text("marker\n", encoding="utf-8")

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "red-ok"


# ─── (c) python project: the pytest path is untouched ────────────────────


class TestPythonPathUntouched:
    def test_pyproject_python_project_still_runs_pytest(
        self, tmp_path: Path
    ) -> None:
        repo = _new_repo(tmp_path, "pyrepo")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "pyproj"\n', encoding="utf-8"
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

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        # a real pytest run on both trees — not runner-unsupported
        assert result.verdict == "red-ok"
        assert result.exit_code == 0
        assert result.copied_files == ["tests/test_calc.py"]
        assert "AssertionError" in result.baseline_output

    def test_no_manifest_still_defaults_to_pytest(self, tmp_path: Path) -> None:
        repo = _new_repo(tmp_path, "bare")
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        _commit(repo, "buggy add")
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        _write_todo(repo, 'prove_red: ["tests/test_calc.py"]\n')
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "red-ok"
        assert result.exit_code == 0


# ─── (b2) custom command: "the check did not run" on the baseline ───────


class TestCommandNotRunOnBaseline:
    """A baseline where the command did not run is broken-runner, never a
    red. The old code read the timeout's rc 2 and a missing command's rc
    127 as a plain failure: when the current tree passed, that was a
    false red-ok (the command was killed/absent, it did not fail)."""

    def test_baseline_timeout_is_broken_runner(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import awf.prove_red as pr

        repo = _new_repo(tmp_path, "hangrepo")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "hangproj"\n', encoding="utf-8"
        )
        _commit(repo, "hang baseline")
        # the command hangs in the baseline worktree (no marker there) and
        # passes in the current tree (marker present, untracked)
        (repo / "hang.marker").write_text("marker\n", encoding="utf-8")
        _write_todo(
            repo,
            'prove_red_cmd: '
            '"if [ -f hang.marker ]; then exit 0; else sleep 5; fi"\n',
        )
        _write_baseline(repo)
        monkeypatch.setattr(pr, "PYTEST_RUN_TIMEOUT", 1)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "broken-runner"
        assert result.exit_code == 2
        assert "did not run (timeout)" in result.message

    def test_baseline_command_not_found_is_broken_runner(
        self, tmp_path: Path
    ) -> None:
        repo = _new_repo(tmp_path, "nfcmd")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "nfcmdproj"\n', encoding="utf-8"
        )
        _commit(repo, "no-script baseline")
        # the script exists only in the current tree: in the baseline
        # worktree `./fix.sh` is not found — shell rc 127
        script = repo / "fix.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
        _write_todo(repo, 'prove_red_cmd: "./fix.sh"\n')
        _write_baseline(repo)

        result = prove_red(repo, "TODO-0001", tmp_base=tmp_path / "wt")

        assert result.verdict == "broken-runner"
        assert result.exit_code == 2
        assert "127" in result.message
        assert "not a red" in result.message
