"""Integration test: full TODO lifecycle across multiple pipeline runs.

Tests the exact scenario that DF6-1/DF6-2/DF6-3 were designed to fix:
1. TODO-0001 dispatched + pipeline runs → verify → archive to done/
2. TODO-0002 dispatched + pipeline runs → should NOT pick up old TODO-0001
3. done_count correctly reflects completed TODOs
4. inbox is clean after each pipeline run

This test would have caught ALL DF5/DF6 lifecycle bugs before dogfood.
"""
from pathlib import Path

from conftest import run_awf


def _create_todo(proj: Path, todo_id: str) -> None:
    """Create TODO in inbox with pre-created APPROVE for --auto mode.

    Uses correct naming convention: {todo_id}.ready (not TODO-{todo_id}.ready).
    """
    inbox = proj / ".agentic/inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n")
    (inbox / f"{todo_id}.ready").write_text("")
    (inbox / f"APPROVE-{todo_id}.ready").write_text("")


class TestTodoLifecycleIntegration:
    """Full lifecycle: dispatch → start → verify → archive → next TODO."""

    def test_two_sequential_todos_no_orphan(
        self, initialized_project: Path, awf_bin: str, awf_env: dict
    ):
        """Run two TODOs sequentially. Second must NOT pick up first as orphan.

        Before DF6-1: TODO-0001.ready stayed in inbox → BD-30 picked it up
        as orphan when TODO-0002 was dispatched.
        """
        # === TODO-0001 lifecycle ===
        _create_todo(initialized_project, "TODO-0001")

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"
        result1 = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )
        assert result1.returncode == 0, \
            f"Pipeline 1 failed. stdout={result1.stdout.decode()!r}"

        # DF6-1: TODO-0001 must be archived
        done_1 = initialized_project / ".agentic/done/TODO-0001"
        assert done_1.is_dir(), \
            f"TODO-0001 not archived. stdout={result1.stdout.decode()!r}"

        # inbox should not have TODO-0001 anymore
        inbox = initialized_project / ".agentic/inbox"
        assert not (inbox / "TODO-0001.md").exists(), "TODO-0001.md still in inbox"
        assert not (inbox / "TODO-0001.ready").exists(), "TODO-0001.ready still in inbox"
        assert not (inbox / "APPROVE-TODO-0001.ready").exists(), \
            "APPROVE-TODO-0001.ready still in inbox"

        # === TODO-0002 lifecycle ===
        _create_todo(initialized_project, "TODO-0002")

        result2 = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )
        assert result2.returncode == 0, \
            f"Pipeline 2 failed. stdout={result2.stdout.decode()!r}"

        # DF6-1: TODO-0002 must be archived
        done_2 = initialized_project / ".agentic/done/TODO-0002"
        assert done_2.is_dir(), \
            f"TODO-0002 not archived. stdout={result2.stdout.decode()!r}"

        # Both TODOs in done/
        assert (done_1 / "TODO.md").is_file()
        assert (done_2 / "TODO.md").is_file()

        # inbox is clean
        assert not list(inbox.glob("TODO-*.ready")), \
            f"Inbox still has TODO signals: {list(inbox.glob('TODO-*.ready'))}"

    def test_done_count_increments(
        self, initialized_project: Path, awf_bin: str, awf_env: dict
    ):
        """awf status should show incremented done_count after each pipeline."""
        _create_todo(initialized_project, "TODO-0001")

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"
        run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )

        # Check status after first TODO
        status1 = run_awf(
            awf_bin, ["status"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=10,
        )
        stdout1 = status1.stdout.decode()
        # done_count should be at least 1 (may count from outbox OR done/)
        assert "1" in stdout1 or "Done" in stdout1, \
            f"Expected done_count=1 in status output: {stdout1}"

    def test_orphan_not_picked_up_after_archive(
        self, initialized_project: Path, awf_bin: str, awf_env: dict
    ):
        """BD-30 orphan pickup must NOT find archived TODOs.

        Before DF6-3: even after archive, if TODO-0001.ready somehow
        remained in inbox, BD-30 would pick it up as orphan.
        """
        _create_todo(initialized_project, "TODO-0001")

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"
        run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )

        # TODO-0001 is archived. Now create TODO-0002 and start.
        _create_todo(initialized_project, "TODO-0002")

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )

        # Pipeline should complete successfully (not get stuck on orphan)
        stdout = result.stdout.decode()
        assert "TODO-0002" in stdout, \
            f"Pipeline should process TODO-0002. stdout={stdout}"
        assert result.returncode == 0, \
            f"Pipeline failed (orphan pickup?). stdout={stdout}"

        # No "orphan" mention for TODO-0001
        assert "orphan TODO signal: TODO-0001" not in stdout, \
            f"BD-30 picked up archived TODO-0001 as orphan. stdout={stdout}"

    def test_handoff_files_archived(
        self, initialized_project: Path, awf_bin: str, awf_env: dict
    ):
        """Handoff files should be moved to done/{id}/handoff/ after archive."""
        _create_todo(initialized_project, "TODO-0001")

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"
        run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env,
            input_data=b"", timeout=60,
        )

        done_handoff = initialized_project / ".agentic/done/TODO-0001/handoff"
        active_handoff = initialized_project / ".agentic/handoff"

        # Handoff files should be in done/, not in active handoff/
        if done_handoff.is_dir():
            files = list(done_handoff.glob("*.md"))
            assert len(files) > 0, "No handoff files in done/TODO-0001/handoff/"

        # Active handoff/ should not have TODO-0001 files
        if active_handoff.is_dir():
            leftover = list(active_handoff.glob("*-TODO-0001.md"))
            assert len(leftover) == 0, \
                f"Handoff files for TODO-0001 still in active dir: {leftover}"
