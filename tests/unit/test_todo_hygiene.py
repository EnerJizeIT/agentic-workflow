"""RUN3 #4/#5: state hygiene — unblock (stale closures) + todo-remove.

Battle case: TODO-0018 was re-dispatched while a stale
``outbox/BLOCKED-TODO-0018.ready`` survived — ``todos.is_closed`` kept the
fresh TODO invisible to ``awf_status``/``awf_start``. ``unblock_todo`` lifts
BLOCKED/ACK closures (DONE stays closed — only ``awf restore`` lifts it);
``remove_todo`` deletes a TODO that never started, leaving a trace in
``done/<id>/removed-<ts>.md``; ``dispatch_todo`` clears stale BLOCKED/ACK
closures automatically on re-dispatch of the same number.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, paths, todos
from awf.api import hygiene


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    """Git repo with a minimal .agentic/ skeleton."""
    ag = tmp_git_repo / ".agentic"
    ag.mkdir()
    for d in ("inbox", "outbox", "context", "done", "logs"):
        (ag / d).mkdir()
    return tmp_git_repo


@pytest.fixture
def initialized_project(project: Path) -> Path:
    """project after api.init_project (config.yaml present).

    init_project resets the runtime dirs (R1), so re-create them.
    """
    api.init_project(project, project_name="Hygiene")
    ag = project / ".agentic"
    for d in ("inbox", "outbox", "context", "done", "logs"):
        (ag / d).mkdir(parents=True, exist_ok=True)
    return project


def _arm(project: Path, todo_id: str) -> None:
    """Give the TODO a .md + .ready so it is dispatch-shaped."""
    inbox = paths.inbox(project)
    (inbox / f"{todo_id}.md").write_text("task body")
    (inbox / f"{todo_id}.ready").touch()


# ─── A: unblock_todo ────────────────────────────────────────────────────


class TestUnblock:
    def test_stale_blocked_canonical_lifts_closure(self, project):
        _arm(project, "TODO-0001")
        outbox = paths.outbox(project)
        (outbox / "BLOCKED-TODO-0001.md").write_text("worker said: blocked")
        (outbox / "BLOCKED-TODO-0001.ready").touch()
        assert todos.is_closed(paths.inbox(project), outbox, "TODO-0001")

        result = api.unblock_todo(project, "TODO-0001")

        assert not (outbox / "BLOCKED-TODO-0001.ready").exists()
        assert not (outbox / "BLOCKED-TODO-0001.md").exists()
        assert not todos.is_closed(paths.inbox(project), outbox, "TODO-0001")
        assert "TODO-0001" in todos.list_active_todos(paths.inbox(project), outbox)
        # trace remains under context/
        assert result.trace
        trace = project / result.trace
        assert trace.is_dir()
        assert (trace / "BLOCKED-TODO-0001.ready").is_file()
        assert (trace / "BLOCKED-TODO-0001.md").is_file()
        assert "worker said: blocked" in (trace / "BLOCKED-TODO-0001.md").read_text()

    def test_legacy_short_form_lifted(self, project):
        _arm(project, "TODO-0001")
        outbox = paths.outbox(project)
        (outbox / "BLOCKED-0001.md").write_text("legacy reason")
        (outbox / "BLOCKED-0001.ready").touch()

        api.unblock_todo(project, "TODO-0001")

        assert not (outbox / "BLOCKED-0001.ready").exists()
        assert not (outbox / "BLOCKED-0001.md").exists()
        assert not todos.is_closed(paths.inbox(project), outbox, "TODO-0001")

    def test_ack_in_inbox_lifted(self, project):
        _arm(project, "TODO-0001")
        inbox = paths.inbox(project)
        outbox = paths.outbox(project)
        (inbox / "ACK-TODO-0001.ready").write_text("decision: rollback")
        (inbox / "ACK-0001.ready").touch()
        assert todos.is_closed(inbox, outbox, "TODO-0001")

        api.unblock_todo(project, "TODO-0001")

        assert not (inbox / "ACK-TODO-0001.ready").exists()
        assert not (inbox / "ACK-0001.ready").exists()
        assert "TODO-0001" in todos.list_active_todos(inbox, outbox)

    def test_no_closures_is_clear_error(self, project):
        _arm(project, "TODO-0001")
        with pytest.raises(api.AwfApiError, match="nothing to unblock"):
            api.unblock_todo(project, "TODO-0001")

    def test_done_is_never_touched(self, project):
        _arm(project, "TODO-0001")
        outbox = paths.outbox(project)
        (outbox / "DONE-TODO-0001.ready").touch()
        (outbox / "BLOCKED-TODO-0001.ready").touch()

        api.unblock_todo(project, "TODO-0001")

        # BLOCKED gone, DONE stays — the TODO is still closed by DONE
        assert not (outbox / "BLOCKED-TODO-0001.ready").exists()
        assert (outbox / "DONE-TODO-0001.ready").exists()
        assert todos.is_closed(paths.inbox(project), outbox, "TODO-0001")

    def test_invalid_id_rejected(self, project):
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.unblock_todo(project, "garbage")

    def test_refuses_while_pipeline_running(self, project, monkeypatch):
        _arm(project, "TODO-0001")
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda *a, **k: (True, 1234, "")
        )
        with pytest.raises(api.AwfApiError, match="pipeline is running"):
            api.unblock_todo(project, "TODO-0001")


# ─── A: dispatch auto-clear on re-dispatch ──────────────────────────────


class TestDispatchAutoClear:
    def test_reissue_clears_stale_blocked(self, initialized_project):
        outbox = paths.outbox(initialized_project)
        (outbox / "BLOCKED-TODO-0001.md").write_text("stale reason")
        (outbox / "BLOCKED-TODO-0001.ready").touch()

        result = api.dispatch_todo(
            initialized_project, "# TODO-0001\nre-issued", todo_id="TODO-0001"
        )

        assert result.todo_id == "TODO-0001"
        assert not (outbox / "BLOCKED-TODO-0001.ready").exists()
        assert not todos.is_closed(
            paths.inbox(initialized_project), outbox, "TODO-0001"
        )
        assert "TODO-0001" in todos.list_active_todos(
            paths.inbox(initialized_project), outbox
        )
        # log line with the re-dispatch marker
        log = paths.logs_dir(initialized_project) / "orchestrator.log"
        assert log.is_file()
        assert "stale closure cleared on re-dispatch" in log.read_text()

    def test_reissue_refuses_done_closure(self, initialized_project):
        outbox = paths.outbox(initialized_project)
        (outbox / "DONE-TODO-0001.ready").touch()

        with pytest.raises(api.AwfApiError, match="restore"):
            api.dispatch_todo(
                initialized_project, "# TODO-0001\nre-issued", todo_id="TODO-0001"
            )

        # no orphan .md left behind, DONE untouched
        assert not (paths.inbox(initialized_project) / "TODO-0001.md").exists()
        assert (outbox / "DONE-TODO-0001.ready").exists()

    def test_battle_case_status_sees_reissued_todo(self, initialized_project):
        """RUN3 #4: re-issue after stale BLOCKED → awf_status sees the TODO."""
        outbox = paths.outbox(initialized_project)
        (outbox / "BLOCKED-TODO-0018.ready").touch()

        status = api.get_status(initialized_project)
        assert [t["todo_id"] for t in status.active_todos] == []

        api.dispatch_todo(
            initialized_project, "# TODO-0018\nre-issued", todo_id="TODO-0018"
        )

        status = api.get_status(initialized_project)
        assert [t["todo_id"] for t in status.active_todos] == ["TODO-0018"]


# ─── B: remove_todo ─────────────────────────────────────────────────────


class TestRemoveTodo:
    def test_never_started_removed_with_trace(self, project):
        (paths.inbox(project) / "TODO-0002.md").write_text("never started")

        result = api.remove_todo(project, "TODO-0002")

        assert not (paths.inbox(project) / "TODO-0002.md").exists()
        assert result.trace_path
        trace = project / result.trace_path
        assert trace.is_file()
        assert trace.parent.name == "TODO-0002"
        assert trace.name.startswith("removed-")
        assert "never started" in trace.read_text()
        # log line
        log = paths.logs_dir(project) / "orchestrator.log"
        assert log.is_file()
        assert "todo-remove: TODO-0002" in log.read_text()

    def test_refuses_with_dispatch_ready(self, project):
        _arm(project, "TODO-0002")
        with pytest.raises(api.AwfApiError, match="awf unblock|reset --orphans"):
            api.remove_todo(project, "TODO-0002")
        assert (paths.inbox(project) / "TODO-0002.md").exists()

    def test_refuses_with_outbox_signal(self, project):
        (paths.inbox(project) / "TODO-0002.md").write_text("body")
        (paths.outbox(project) / "PROGRESS-TODO-0002.md").write_text("half done")
        with pytest.raises(api.AwfApiError, match="signals"):
            api.remove_todo(project, "TODO-0002")
        assert (paths.inbox(project) / "TODO-0002.md").exists()

    def test_refuses_blocked_with_unblock_hint(self, project):
        (paths.inbox(project) / "TODO-0002.md").write_text("body")
        (paths.outbox(project) / "BLOCKED-TODO-0002.ready").touch()
        with pytest.raises(api.AwfApiError, match="awf unblock"):
            api.remove_todo(project, "TODO-0002")

    def test_refuses_done_with_restore_hint(self, project):
        (paths.inbox(project) / "TODO-0002.md").write_text("body")
        (paths.outbox(project) / "DONE-TODO-0002.ready").touch()
        with pytest.raises(api.AwfApiError, match="restore"):
            api.remove_todo(project, "TODO-0002")

    def test_missing_todo_is_error(self, project):
        with pytest.raises(api.AwfApiError, match="not found"):
            api.remove_todo(project, "TODO-0099")

    def test_repeat_call_is_error(self, project):
        (paths.inbox(project) / "TODO-0002.md").write_text("body")
        api.remove_todo(project, "TODO-0002")
        with pytest.raises(api.AwfApiError, match="not found"):
            api.remove_todo(project, "TODO-0002")

    def test_does_not_touch_longer_id(self, project):
        """TODO-0001 vs TODO-00010: removing 0001 must not see 00010's files."""
        inbox = paths.inbox(project)
        (inbox / "TODO-0001.md").write_text("short id")
        (inbox / "TODO-00010.md").write_text("long id")
        (paths.outbox(project) / "DONE-TODO-00010.ready").touch()

        api.remove_todo(project, "TODO-0001")

        assert not (inbox / "TODO-0001.md").exists()
        assert (inbox / "TODO-00010.md").exists()
        assert (paths.outbox(project) / "DONE-TODO-00010.ready").exists()

    def test_invalid_id_rejected(self, project):
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.remove_todo(project, "TODO-1")
