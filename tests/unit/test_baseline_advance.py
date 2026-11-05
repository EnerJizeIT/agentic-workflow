"""A64: the baseline is taken at stage start, not at dispatch.

``awf dispatch`` snapshots ``BASELINE-<todo>.sha`` when the unit is queued.
A unit dispatched before the previous unit's commit starts with a stale
baseline and its diff swallows other units' files. The engine now moves the
baseline to HEAD on the unit's first pass, but only on a clean tree — a
dirty tree keeps the recorded SHA (otherwise someone's edits vanish from
the diff silently).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import _git_init  # AUD12-08: shared git boilerplate

from awf.pipeline_engine import _ensure_baseline_sha, _is_first_unit_pass


def _git(proj: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=proj, check=True, capture_output=True, text=True
    ).stdout.strip()


def _project(tmp_path: Path) -> Path:
    """A git repo with a committed `.gitignore` that hides `.agentic/`.

    Hiding the artifacts keeps the tree clean while the baseline file
    exists — the condition the shift is allowed to act on.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    _git_init(proj)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    _git(proj, "add", ".gitignore")
    _git(proj, "commit", "-qm", "ignore agentic")
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _commit(proj: Path, name: str, text: str) -> str:
    (proj / name).write_text(text, encoding="utf-8")
    _git(proj, "add", name)
    _git(proj, "commit", "-qm", f"add {name}")
    return _git(proj, "rev-parse", "HEAD")


def _baseline(proj: Path) -> Path:
    return proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha"


def _log_text(proj: Path) -> str:
    log = proj / ".agentic" / "logs" / "orchestrator.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


class TestEnsureBaselineSha:

    def test_creates_baseline_when_missing(self, tmp_path: Path) -> None:
        """Empty context: baseline is created from HEAD (П3 regression)."""
        proj = _project(tmp_path)
        head = _git(proj, "rev-parse", "HEAD")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).read_text(encoding="utf-8") == head + "\n"

    def test_advances_baseline_on_clean_tree(self, tmp_path: Path) -> None:
        """Clean tree + recorded ancestor + HEAD ahead → baseline = HEAD."""
        proj = _project(tmp_path)
        old = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(old + "\n", encoding="utf-8")
        new = _commit(proj, "a.txt", "a\n")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).read_text(encoding="utf-8") == new + "\n"
        log = _log_text(proj)
        assert (
            f"baseline advanced TODO-0001 {old[:8]} -> {new[:8]} (tree clean)"
            in log
        )

    def test_keeps_baseline_on_dirty_tree(self, tmp_path: Path) -> None:
        """Dirty tree: the file stays byte-for-byte — edits stay visible."""
        proj = _project(tmp_path)
        old = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(old + "\n", encoding="utf-8")
        _commit(proj, "a.txt", "a\n")
        (proj / "a.txt").write_text("uncommitted\n", encoding="utf-8")
        before = _baseline(proj).read_bytes()

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).read_bytes() == before
        assert "baseline advanced" not in _log_text(proj), (
            "a dirty tree must not even CLAIM an advance in the log"
        )

    def test_keeps_baseline_when_not_ancestor(self, tmp_path: Path) -> None:
        """A SHA unrelated to HEAD is not adopted (rewritten history)."""
        proj = _project(tmp_path)
        fake = "0" * 40
        _baseline(proj).write_text(fake + "\n", encoding="utf-8")
        _commit(proj, "a.txt", "a\n")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).read_text(encoding="utf-8") == fake + "\n"

    def test_does_not_rewrite_when_baseline_equals_head(
        self, tmp_path: Path
    ) -> None:
        """Baseline == HEAD: no write, so mtime and content stay put."""
        proj = _project(tmp_path)
        head = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(head + "\n", encoding="utf-8")
        mtime_before = _baseline(proj).stat().st_mtime_ns

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).stat().st_mtime_ns == mtime_before
        assert _baseline(proj).read_text(encoding="utf-8") == head + "\n"

    def test_skips_when_not_git_repo(self, tmp_path: Path) -> None:
        """Non-git project: logged skip, no baseline file."""
        proj = tmp_path / "plain"
        (proj / ".agentic" / "context").mkdir(parents=True)
        (proj / ".agentic" / "logs").mkdir(parents=True)

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert not _baseline(proj).exists()
        assert "skip baseline" in _log_text(proj)

    def test_not_git_repo_with_baseline_keeps_it_quietly(
        self, tmp_path: Path
    ) -> None:
        """A legacy "(not a git repo)" baseline is kept without a failure line."""
        proj = tmp_path / "plain"
        (proj / ".agentic" / "context").mkdir(parents=True)
        (proj / ".agentic" / "logs").mkdir(parents=True)
        _baseline(proj).write_text("(not a git repo)\n", encoding="utf-8")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        assert _baseline(proj).read_text(encoding="utf-8") == "(not a git repo)\n"
        assert "advance failed" not in _log_text(proj)

    def test_resume_from_middle_does_not_advance(self, tmp_path: Path) -> None:
        """A resume mid-pipeline keeps the baseline: unit work may be in tree."""
        proj = _project(tmp_path)
        old = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(old + "\n", encoding="utf-8")
        _commit(proj, "a.txt", "a\n")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=False
        )

        assert _baseline(proj).read_text(encoding="utf-8") == old + "\n"
        assert "baseline advanced" not in _log_text(proj)


class TestFirstUnitPass:

    @staticmethod
    def _stages():
        return [
            SimpleNamespace(role="supervisor", kind="plan"),
            SimpleNamespace(role="agent-implementer", kind="execute"),
            SimpleNamespace(role="agent-qa-review", kind="execute"),
        ]

    def test_first_agent_stage_is_first_pass(self) -> None:
        assert _is_first_unit_pass(self._stages(), 1, [0, 0, 0])

    def test_middle_stage_is_not_first_pass(self) -> None:
        assert not _is_first_unit_pass(self._stages(), 2, [0, 0, 0])

    def test_retried_stage_is_not_first_pass(self) -> None:
        assert not _is_first_unit_pass(self._stages(), 1, [0, 1, 0])

    def test_pipeline_without_agent_stage_is_not_first_pass(self) -> None:
        stages = [SimpleNamespace(role="supervisor", kind="plan")]
        assert not _is_first_unit_pass(stages, 0, [0])


class TestEngineWiring:
    """F6: ``execute_agent_stage`` must hand the real predicate to the helper.

    The helper's own tests cannot see the call site: a dropped
    ``first_pass=`` argument would leave every one of them green while the
    pipeline shifted the baseline on every stage. These two go through the
    public stage entry with a real git repo, so the effect is the file
    content, not a recorded kwarg.
    """

    @staticmethod
    def _stages():
        from awf.pipeline import Stage

        return [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="worker", role="agent-implementer", kind="execute"),
            Stage(name="qa", role="agent-qa-review", kind="execute"),
        ]

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        proj = _project(tmp_path)
        for name in ("inbox", "outbox"):
            (proj / ".agentic" / name).mkdir()
        (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text(
            "# TODO-0001\n", encoding="utf-8"
        )
        old = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(old + "\n", encoding="utf-8")
        _commit(proj, "other.txt", "another unit\n")
        return proj

    @staticmethod
    def _run(proj: Path, tmp_path: Path, monkeypatch, stage_idx: int):
        from awf import pipeline_engine, signals, verify

        def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kw):
            outbox = project_dir / ".agentic" / "outbox"
            (outbox / f"DONE-{todo_id}.md").write_text("# done\n", encoding="utf-8")
            (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", fake_worker)
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(signals, "clean_stage_signals", lambda *a, **kw: None)
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

        stages = TestEngineWiring._stages()
        pipeline_engine.execute_agent_stage(
            stage=stages[stage_idx], current_todo="TODO-0001",
            project_dir=proj, config={}, logs_dir=proj / ".agentic" / "logs",
            stages=stages, stage_idx=stage_idx, retry_counts=[0, 0, 0],
            auto=False, agent_hard_timeout=None, pipeline_name=None,
        )

    def test_first_agent_stage_advances_stale_baseline(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        proj = self._project(tmp_path)
        head = _git(proj, "rev-parse", "HEAD")

        self._run(proj, tmp_path, monkeypatch, stage_idx=1)

        assert _baseline(proj).read_text(encoding="utf-8") == head + "\n"
        assert "baseline advanced" in _log_text(proj)

    def test_later_stage_keeps_baseline(self, tmp_path: Path, monkeypatch) -> None:
        """A mid-pipeline stage starts after the unit's work exists: no shift."""
        proj = self._project(tmp_path)
        baseline_before = _baseline(proj).read_bytes()

        self._run(proj, tmp_path, monkeypatch, stage_idx=2)

        assert _baseline(proj).read_bytes() == baseline_before
        assert "baseline advanced" not in _log_text(proj)


class TestDispatchShapedBaseline:
    """The writer and the shifter agree on the artefact (dispatch cell).

    Every other test hand-writes ``.sha``. ``awf dispatch`` writes the sha
    together with ``.status``/``.untracked``/``.tests.log``, so the shift
    must work on that real set and keep the one-sha format.
    """

    def test_real_baseline_from_dispatch_is_advanced(self, tmp_path: Path) -> None:
        from awf.api.pipeline import create_baseline

        proj = tmp_path / "proj"
        proj.mkdir()
        _git_init(proj)
        (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
        _git(proj, "add", ".gitignore")
        _git(proj, "commit", "-qm", "ignore agentic")
        (proj / ".agentic" / "context").mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: probe\n", encoding="utf-8"
        )

        create_baseline(proj, "TODO-0001", run_tests=False)
        old = _git(proj, "rev-parse", "HEAD")
        assert _baseline(proj).read_text(encoding="utf-8") == old + "\n"
        assert (proj / ".agentic" / "context" / "BASELINE-TODO-0001.status").read_text(
            encoding="utf-8"
        ) == ""

        new = _commit(proj, "other-unit.txt", "another unit\n")
        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )

        written = _baseline(proj).read_text(encoding="utf-8")
        assert written == new + "\n"
        assert len(written.splitlines()) == 1, "one sha per file — format is fixed"
        assert (
            f"П3: baseline advanced TODO-0001 {old[:8]} -> {new[:8]} (tree clean)"
            in _log_text(proj)
        )
        # The point of the shift: the other unit's commit is no longer part
        # of THIS unit's diff.
        assert _git(proj, "diff", "--name-only", new) == ""
        assert "other-unit.txt" in _git(proj, "diff", "--name-only", old)


class TestRollbackSurface:
    """A64 changes what ``rollback`` resets to — nobody's table listed it.

    ``awf/api/pipeline.rollback`` reads ``BASELINE-<id>.sha`` at call time.
    Before A64 that was dispatch time, so undoing a unit ran
    ``git reset --hard`` over every commit a predecessor made in between.
    After the shift it is the stage-start HEAD: the unit's own tracked work
    goes, the predecessor's commit stays.
    """

    def test_hard_rollback_keeps_predecessor_commit(self, tmp_path: Path) -> None:
        from awf.api.pipeline import rollback

        proj = _project(tmp_path)
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: probe\n", encoding="utf-8"
        )
        dispatch_sha = _git(proj, "rev-parse", "HEAD")
        _baseline(proj).write_text(dispatch_sha + "\n", encoding="utf-8")
        predecessor_commit = _commit(proj, "predecessor.txt", "shipped earlier\n")

        _ensure_baseline_sha(
            proj, "TODO-0001", proj / ".agentic" / "logs", first_pass=True
        )
        (proj / "predecessor.txt").write_text("unit edit\n", encoding="utf-8")

        rollback(proj, "TODO-0001", mode="hard")

        assert _git(proj, "rev-parse", "HEAD") == predecessor_commit
        assert predecessor_commit != dispatch_sha
        assert (proj / "predecessor.txt").read_text(encoding="utf-8") == (
            "shipped earlier\n"
        ), "the unit's rollback must not erase the predecessor's commit"
