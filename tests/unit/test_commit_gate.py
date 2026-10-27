"""RUN5 #1 (TODO-0052): commit-gate view of the leak-gate.

The gate itself (awf/commit_gate.py::_files_changed_since_baseline) is
unchanged: it commits untracked files that are NOT listed in
BASELINE-<todo>.untracked. The leak-gate works by removing the carried-over
paths from that list at baseline time (awf/api/dispatch.py, Part A.2) — so
these tests prove the end-to-end consequence:

- a carried-over file (absent from the retry's baseline-untracked) lands in
  the commit;
- a genuinely pre-existing untracked file (listed there) is still excluded;
- ``maybe_commit`` commits exactly that split.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.commit_gate import maybe_commit
from awf.commit_plan import _files_changed_since_baseline

TODO = "TODO-0002"


@pytest.fixture
def gate_repo(tmp_git_repo: Path) -> tuple[Path, str]:
    """Committed repo + BASELINE-{TODO}.untracked snapshot (empty gitignore
    so .agentic/ does not pollute the untracked list)."""
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_git_repo, sha


class TestCarriedOverFiles:
    def test_carried_over_file_included(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        # The retry's baseline did NOT list src/a.py (carry-over excluded it).
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert "src/a.py" in files, (
            "RUN5 #1: the retry must commit the file the rejected attempt left behind"
        )
        assert "stale.txt" not in files, (
            "genuinely pre-existing untracked must stay out of the unit commit"
        )

    def test_pre_existing_only_is_still_excluded(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert files == []

    def test_without_baseline_snapshot_all_untracked_included(
        self, gate_repo: tuple[Path, str]
    ) -> None:
        """Legacy path (no BASELINE-*.untracked at all): gate includes every
        untracked file — a carry-over file is committed there too."""
        repo, sha = gate_repo
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert "src/a.py" in files

    def test_maybe_commit_commits_carried_over_not_pre_existing(
        self, gate_repo: tuple[Path, str]
    ) -> None:
        repo, sha = gate_repo
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")
        (repo / "stale.txt").write_text("pre-existing\n")
        logs = repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        ok = maybe_commit("implement", TODO, "commit_and_report", repo, logs,
                          auto=False, baseline_sha=sha)
        assert ok.status == "committed", f"the unit commit must commit — got {ok.status}: {ok.reason}"
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert "src/a.py" in committed
        assert "stale.txt" not in committed
        # stale.txt stays in the tree, untouched
        assert (repo / "stale.txt").is_file()
        # R-03-F1: the commit ran on the isolated index, then the real
        # index is synced for the plan's files — src/a.py matches HEAD
        # (no longer "untracked" in the user's index view); stale.txt,
        # outside the plan, stays untracked.
        head_files = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert "src/a.py" in head_files
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert "stale.txt" in untracked
        assert "src/a.py" not in untracked, (
            "R-03-F1: after the unit commit the plan's files must match "
            "HEAD in the real index"
        )


class TestIncludeUntracked:
    """RUN10 #4 (TODO-0074): commit-gate view of include_untracked.

    The gate is unchanged: it commits untracked files that are NOT listed
    in BASELINE-<todo>.untracked. include_untracked works by removing the
    re-claimed paths from that list at baseline time (awf/api/dispatch.py)
    — same mechanism as the leak-gate carry-over."""

    def test_included_file_lands_in_files_changed(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        # The baseline did NOT list x.md (include_untracked excluded it).
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "x.md").write_text("pre-existing, re-claimed\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert "x.md" in files, (
            "RUN10 #4: a consciously included pre-existing file joins the unit commit"
        )
        assert "stale.txt" not in files, "the other pre-existing file stays out"

    def test_not_included_stays_excluded(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text(
            "x.md\nstale.txt\n"
        )
        (repo / "x.md").write_text("pre-existing\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert files == [], "default behavior: pre-existing untracked stays excluded"


class TestCorruptViolationRecord:
    """CARRY IMP3 (carry-cd R11, AC28–AC29): повреждённая запись нарушения
    закрывает коммит.

    Не-dict ``paths`` нельзя доказать исчезнувшим — fail-closed: запись
    остаётся открытой под синтетическим именем ``<paths: invalid>``, а
    слияние новой записи поверх не теряет повреждение.
    """

    def _record(self, repo: Path, paths, *, todo: str = TODO) -> Path:
        import json

        ctx = repo / ".agentic" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        record = {
            "todo_id": todo,
            "stage": "worker",
            "recorded_at": "2026-10-27T00:00:00+00:00",
            "allowlist": [],
            "paths": paths,
            "error": "",
        }
        target = ctx / f"WRITE-VIOLATION-{todo}.json"
        target.write_text(json.dumps(record), encoding="utf-8")
        return target

    def _head(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_non_dict_paths_is_open_and_refuses_commit(self, gate_repo):
        from awf import stage_manifest

        repo, sha = gate_repo
        self._record(repo, "oops")

        open_ones = stage_manifest.open_violations(repo)
        assert [v.paths for v in open_ones] == [("<paths: invalid>",)]
        refusal = stage_manifest.violation_refusal(repo)
        assert "<paths: invalid>" in refusal
        assert f"WRITE-VIOLATION-{TODO}.json" in refusal

        head_before = self._head(repo)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("unit work\n", encoding="utf-8")
        logs = repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        outcome = maybe_commit(
            "implement", TODO, "commit_and_report", repo, logs,
            auto=False, baseline_sha=sha,
        )
        assert outcome.status == "refused", (
            f"a corrupt violation record must refuse the commit — got "
            f"{outcome.status}: {outcome.reason}"
        )
        assert "<paths: invalid>" in outcome.reason, (
            f"the refusal must name the corrupt record's synthetic path: {outcome.reason}"
        )
        assert f"WRITE-VIOLATION-{TODO}.json" in outcome.reason, (
            f"the refusal must name the record file to delete: {outcome.reason}"
        )
        assert self._head(repo) == head_before
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, capture_output=True,
        )
        assert staged.returncode == 0, "a refused commit must not stage anything"

    def test_empty_paths_names_no_path(self, gate_repo):
        from awf import stage_manifest

        repo, _sha = gate_repo
        self._record(repo, [])

        open_ones = stage_manifest.open_violations(repo)
        assert len(open_ones) == 1
        assert open_ones[0].paths == ()
        assert "record names no path" in stage_manifest.violation_refusal(repo)

    def test_path_without_after_stays_open(self, gate_repo):
        from awf import stage_manifest

        repo, _sha = gate_repo
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("unit work\n", encoding="utf-8")
        self._record(repo, {"src/a.py": {"before": "<missing>"}})

        open_ones = stage_manifest.open_violations(repo)
        assert [v.paths for v in open_ones] == [("src/a.py",)]

    def test_corrupt_record_survives_a_newer_record(self, gate_repo):
        from awf import stage_manifest

        repo, _sha = gate_repo
        self._record(repo, "oops")
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("unit work\n", encoding="utf-8")
        stage_manifest.record_violation(
            repo, TODO, "worker",
            allowlist=(),
            paths_outside=["src/a.py"],
        )
        open_ones = stage_manifest.open_violations(repo)
        paths = {p for v in open_ones for p in v.paths}
        assert "<paths: invalid>" in paths, "merge must not lose the corruption"
        assert "src/a.py" in paths

        (repo / "src" / "a.py").write_text("rolled back\n", encoding="utf-8")
        still = {p for v in stage_manifest.open_violations(repo) for p in v.paths}
        assert still == {"<paths: invalid>"}
