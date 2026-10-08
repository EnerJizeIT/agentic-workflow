"""REPORTS26 F4 (TODO-0164): run_next's started answer contradicts the run loop.

Defect (owner's report ``awf-feature-20261007-...go-idle-protivorecha.md``):
launching a unit INSIDE a run answered ``next_action`` "GO IDLE. Wait for
the verify event..." while the embedded pipeline message carried
"GO IDLE — wait for user." Two protocols in one answer — but inside a run
(забег) the supervisor must NOT go idle: the protocol is the run loop
``awf_wait_for_event(actionable_only=True)`` → verify → approve → done →
``awf_run_next`` (phase-run.md, RUN mode).

Invariants (TODO-0164):
1. ``run_next`` ``started`` inside an active run: neither ``message`` nor
   ``next_action`` contains "GO IDLE"; ``next_action`` names the run loop
   (``awf_wait_for_event`` + run position/TODO). The pipeline facts that
   the standalone message carries (PID, orphan warning) survive.
2. Standalone start (``awf_start`` / ``start_pipeline``) keeps
   "GO IDLE — wait for user." — outside a run that IS the correct protocol.
3. The other ``run_next`` branches (refused/stopped) are untouched.

Findings covered:
- test_run_next_started_has_no_go_idle — invariant 1, prove_red
- test_run_next_started_keeps_pipeline_facts — invariant 1 (facts survive)
- test_standalone_start_keeps_go_idle — invariant 2
- test_refused_branch_text_untouched — invariant 3
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf.api._results import StartResult


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports26F4RunNext")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


# The production form of the standalone background message
# (awf/api/pipeline.py: start_pipeline, background branch).
STANDALONE_MSG = "awf start running in background (PID 4242). GO IDLE — wait for user."


class TestRunNextStartedRunProtocol:
    """Invariants 1+3: inside a run the answer carries the run loop only."""

    def test_run_next_started_has_no_go_idle(self, tmp_git_repo, monkeypatch):
        """Baseline (red): message carried the embedded
        "GO IDLE — wait for user." and next_action said "GO IDLE. Wait for
        the verify event..." — two protocols in one answer."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")
        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])

        def fake_start(project_dir, **kw):
            assert kw.get("background") is True
            return StartResult(
                run_mode="background", run_id=4242,
                log_file=str(proj / ".agentic" / "logs" / "pipeline.log"),
                exit_code=None, message=STANDALONE_MSG,
            )

        monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

        result = api.run_next(proj)

        assert result.action == "started", (
            f"launch inside an active run must be started, got {result.action!r}"
        )
        assert result.todo_id == "TODO-0001"
        assert "GO IDLE" not in result.message, (
            f"message carries the standalone protocol: {result.message!r}"
        )
        assert "GO IDLE" not in result.next_action, (
            f"next_action carries the standalone protocol: {result.next_action!r}"
        )
        assert "run loop" in result.next_action
        assert "awf_wait_for_event" in result.next_action
        assert "awf_run_next" in result.next_action
        assert "TODO-0001" in result.next_action
        assert "1/2" in result.next_action

    def test_run_next_started_keeps_pipeline_facts(self, tmp_git_repo, monkeypatch):
        """Stripping the GO IDLE sentence must not eat the facts the
        standalone message carries: the PID and a prepended orphan warning
        (RUN8 #2) stay in the answer."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])

        orphan = (
            "WARNING: possible orphaned worker PID 999 from a previous kill "
            "is still alive — its edits may land in this unit. Check it "
            "(ps) and stop it manually if it is not part of this start."
        )

        def fake_start(project_dir, **kw):
            return StartResult(
                run_mode="background", run_id=4242,
                log_file=str(proj / "log.txt"), exit_code=None,
                message=orphan + " " + STANDALONE_MSG,
            )

        monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

        result = api.run_next(proj)

        assert result.action == "started"
        assert "GO IDLE" not in result.message
        assert "PID 4242" in result.message
        assert "orphaned worker PID 999" in result.message

    def test_refused_branch_text_untouched(self, tmp_git_repo, monkeypatch):
        """Invariant 3: the refused/stopped branches keep their own texts —
        the fix must touch the started branch only."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        # No TODO-0001.md in the inbox → the launch is refused.

        def fake_start(project_dir, **kw):  # pragma: no cover - must not be called
            raise AssertionError("a refused launch must not reach start_pipeline")

        monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

        result = api.run_next(proj)

        assert result.action == "refused"
        assert "GO IDLE" not in result.next_action
        assert result.next_action == (
            "Write .agentic/inbox/TODO-0001.md (the task for this queue item), "
            "then call awf_run_next again."
        )


class TestStandaloneProtocolKept:
    """Invariant 2: outside a run, GO IDLE is the correct protocol."""

    def test_standalone_start_keeps_go_idle(self, tmp_git_repo, monkeypatch):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        log = proj / ".agentic" / "logs" / "pipeline.log"
        log.parent.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            api_pipeline, "start_in_background",
            lambda *a, **kw: (4242, log, None),
        )
        monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda pid, lf: True)

        result = api_pipeline.start_pipeline(
            proj, background=True, todo_id="TODO-0001"
        )

        assert result.run_mode == "background"
        assert "GO IDLE — wait for user." in result.message
