"""E2E tests for signal handling — canonical, legacy, active TODO selection, orphan reset."""
from pathlib import Path

from conftest import run_awf


def _create_todo(proj: Path, todo_id: str):
    """Create a TODO in inbox."""
    inbox = proj / ".agentic/inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"stub {todo_id}\n")
    (inbox / f"{todo_id}.ready").write_text(f"signal: TASK_READY\ntask_id: {todo_id}\n")


class TestSignals:

    def test_signals_canonical(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """Canonical DONE-TODO-{NNNN}.ready is recognized by awf status."""
        outbox = initialized_project / ".agentic/outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "DONE-TODO-0042.ready").write_text("")

        result = run_awf(
            awf_bin, ["status"],
            cwd=initialized_project, env=awf_env, timeout=30
        )
        assert result.returncode == 0, f"awf status failed: {result.stderr.decode()}"
        stdout = result.stdout.decode()
        # Should show completed count > 0 or not show TODO-0042 as active
        assert "Completed:" in stdout or "Summary" in stdout

    def test_signals_legacy_short(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """Legacy short form DONE-{NNNN}.ready is honored — no crash, no false active."""
        inbox = initialized_project / ".agentic/inbox"
        inbox.mkdir(exist_ok=True)
        outbox = initialized_project / ".agentic/outbox"
        outbox.mkdir(exist_ok=True)

        # Create inbox TODO-0042
        (inbox / "TODO-0042.md").write_text("stub\n")
        (inbox / "TODO-0042.ready").write_text("signal: TASK_READY\ntask_id: TODO-0042\n")
        # Create legacy closure
        (outbox / "DONE-0042.ready").write_text("")

        result = run_awf(
            awf_bin, ["status"],
            cwd=initialized_project, env=awf_env, timeout=30
        )
        assert result.returncode == 0, f"awf status failed: {result.stderr.decode()}"
        stdout = result.stdout.decode()
        # TODO-0042 should NOT appear as active since it's closed by legacy signal
        assert "Active: TODO-0042" not in stdout

    def test_find_active_todo_picks_newest(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """Orchestrator picks the highest-numbered active TODO (not lowest)."""
        inbox = initialized_project / ".agentic/inbox"
        inbox.mkdir(exist_ok=True)

        for n in ["0001", "0002"]:
            (inbox / f"TODO-{n}.md").write_text(f"stub {n}\n")
            (inbox / f"TODO-{n}.ready").write_text(f"signal: TASK_READY\ntask_id: TODO-{n}\n")
            # BD-8: pre-authorize commit so --auto mode doesn't block on approval
            (inbox / f"APPROVE-TODO-{n}.ready").write_text(
                f"signal: APPROVE\ntask_id: TODO-{n}\n"
            )

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=60
        )

        # The stub should have processed TODO-0002 (the newest), not TODO-0001.
        # DF6-1: after verify approve, TODO is archived to done/{todo_id}/
        done_dir = initialized_project / ".agentic/done/TODO-0002"
        assert done_dir.is_dir(), \
            f"Expected done/TODO-0002/ (DF6-1 archive). stderr={result.stderr.decode()!r}"

    def test_reset_orphans(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """awf reset --orphans --force removes orphan TODOs, preserves in-flight and closed."""
        inbox = initialized_project / ".agentic/inbox"
        outbox = initialized_project / ".agentic/outbox"
        inbox.mkdir(exist_ok=True)
        outbox.mkdir(exist_ok=True)

        # TODO-0001: orphan — .ready + .md, no PROGRESS file
        (inbox / "TODO-0001.md").write_text("orphan\n")
        (inbox / "TODO-0001.ready").write_text("signal: TASK_READY\ntask_id: TODO-0001\n")

        # TODO-0002: in-flight — .ready + .md + PROGRESS
        (inbox / "TODO-0002.md").write_text("in-flight\n")
        (inbox / "TODO-0002.ready").write_text("signal: TASK_READY\ntask_id: TODO-0002\n")
        (outbox / "PROGRESS-TODO-0002.md").write_text("## Task 1 - [ ] in progress\n")

        # TODO-0003: closed — .ready + .md + DONE signal
        (inbox / "TODO-0003.md").write_text("closed\n")
        (inbox / "TODO-0003.ready").write_text("signal: TASK_READY\ntask_id: TODO-0003\n")
        (outbox / "DONE-TODO-0003.ready").write_text("")

        result = run_awf(
            awf_bin, ["reset", "--orphans", "--force"],
            cwd=initialized_project, env=awf_env, timeout=30
        )
        assert result.returncode == 0, f"awf reset failed: {result.stderr.decode()}"

        # Orphan removed
        assert not (inbox / "TODO-0001.md").exists(), "Orphan TODO-0001.md should be gone"
        assert not (inbox / "TODO-0001.ready").exists(), "Orphan TODO-0001.ready should be gone"

        # In-flight preserved
        assert (inbox / "TODO-0002.md").exists(), "In-flight TODO-0002.md should remain"
        assert (inbox / "TODO-0002.ready").exists(), "In-flight TODO-0002.ready should remain"

        # Closed preserved
        assert (inbox / "TODO-0003.md").exists(), "Closed TODO-0003.md should remain"
        assert (inbox / "TODO-0003.ready").exists(), "Closed TODO-0003.ready should remain"
        assert (outbox / "DONE-TODO-0003.ready").exists(), "Closed DONE signal should remain"
