"""RUN3 #4/#5 + RUN5 #2: state hygiene — unblock, todo-remove, todo-retire.

Battle case: TODO-0018 was re-dispatched while a stale
``outbox/BLOCKED-TODO-0018.ready`` survived — ``todos.is_closed`` kept the
fresh TODO invisible to ``awf_status``/``awf_start``. ``unblock_todo`` lifts
BLOCKED/ACK closures (DONE stays closed — only ``awf restore`` lifts it);
``remove_todo`` deletes a TODO that never started, leaving a trace in
``done/<id>/removed-<ts>.md``; ``dispatch_todo`` clears stale BLOCKED/ACK
closures automatically on re-dispatch of the same number.

RUN5 #2 battle case: TODO-0035 was rejected via REVIEW — the reject path
writes ``DONE-<id>.{md,json}``/``PROGRESS``/``REVIEW`` to the outbox WITHOUT
the ``DONE-<id>.ready`` signal, so the TODO stays "active" forever.
``retire_todo`` archives it to ``done/<id>/`` with a RETIRED note and no
fake closure signal; ``restore_todo`` still brings it back.
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


# ─── C: retire_todo (RUN5 #2 — rejected/abandoned ghost archive) ────────


class TestRetireTodo:
    """``retire_todo`` — the rejected-TODO ghost (battle case TODO-0035)."""

    def _ghost(self, project: Path, todo_id: str = "TODO-0035") -> None:
        """The reject-path shape: md + .ready in inbox, PROGRESS/DONE
        (md+json, NO .ready)/REVIEW in outbox — exactly what TODO-0035 had.
        """
        inbox = paths.inbox(project)
        outbox = paths.outbox(project)
        (inbox / f"{todo_id}.md").write_text("# rejected task")
        (inbox / f"{todo_id}.ready").touch()
        (outbox / f"PROGRESS-{todo_id}.md").write_text("half done")
        (outbox / f"DONE-{todo_id}.md").write_text("worker claim")
        (outbox / f"DONE-{todo_id}.json").write_text("{}")
        (outbox / f"REVIEW-{todo_id}.md").write_text("rejected: too broad")

    def _write_state(self, project: Path, todo_id: str) -> None:
        from awf import pipeline_state

        pipeline_state.write_state(project, todo_id=todo_id, pipeline_pid=1)

    def test_battle_case_ghost_retired(self, project):
        self._ghost(project)
        inbox, outbox = paths.inbox(project), paths.outbox(project)
        # the ghost is "active" until retired (the bug shape)
        assert "TODO-0035" in todos.list_active_todos(inbox, outbox)

        result = api.retire_todo(project, "TODO-0035", "rejected at verify")

        assert not (inbox / "TODO-0035.md").exists()
        assert not (inbox / "TODO-0035.ready").exists()
        assert not (outbox / "PROGRESS-TODO-0035.md").exists()
        assert not (outbox / "DONE-TODO-0035.md").exists()
        assert not (outbox / "DONE-TODO-0035.json").exists()
        assert not (outbox / "REVIEW-TODO-0035.md").exists()
        assert "TODO-0035" not in todos.list_active_todos(inbox, outbox)
        # archive shape: restore-ready TODO.md + the history files
        done = paths.done_dir(project) / "TODO-0035"
        assert (done / "TODO.md").is_file()
        assert "rejected task" in (done / "TODO.md").read_text()
        assert (done / "TODO-0035.ready").is_file()
        assert (done / "PROGRESS-TODO-0035.md").is_file()
        assert (done / "DONE-TODO-0035.md").is_file()
        assert (done / "DONE-TODO-0035.json").is_file()
        assert (done / "REVIEW-TODO-0035.md").is_file()
        # RETIRED note: reason, who, time, moved-file list
        assert result.retired_note
        note = project / result.retired_note
        assert note.parent == done
        assert note.name.startswith("RETIRED-") and note.name.endswith(".md")
        text = note.read_text()
        assert "TODO-0035" in text
        assert "rejected at verify" in text
        assert "supervisor" in text
        assert ".agentic/inbox/TODO-0035.md" in text
        assert ".agentic/outbox/DONE-TODO-0035.json" in text
        # moved list in the result
        assert ".agentic/done/TODO-0035/TODO.md" in result.moved
        # log line
        log = paths.logs_dir(project) / "orchestrator.log"
        assert "retire: TODO-0035" in log.read_text()

    def test_status_clean_after_retire(self, initialized_project):
        self._ghost(initialized_project)
        status = api.get_status(initialized_project)
        assert [t["todo_id"] for t in status.active_todos] == ["TODO-0035"]

        api.retire_todo(initialized_project, "TODO-0035", "rejected at verify")

        status = api.get_status(initialized_project)
        assert [t["todo_id"] for t in status.active_todos] == []

    def test_restore_after_retire_works(self, project):
        self._ghost(project)
        api.retire_todo(project, "TODO-0035", "rejected at verify")

        result = api.restore_todo(project, "TODO-0035")

        inbox = paths.inbox(project)
        assert (inbox / "TODO-0035.md").is_file()
        assert (inbox / "TODO-0035.ready").is_file()
        assert "TODO-0035" in todos.list_active_todos(
            inbox, paths.outbox(project)
        )
        # history files stay in done/ (not duplicated into the inbox)
        done = paths.done_dir(project) / "TODO-0035"
        assert (done / "DONE-TODO-0035.md").is_file()
        assert "restored" in result.message

    def test_retire_restore_retire_roundtrip_keeps_history(self, project):
        """Reject → retire → restore → re-reject → retire again: the archive
        accumulates (suffixed copies, two RETIRED notes) — nothing overwrites."""
        inbox, outbox = paths.inbox(project), paths.outbox(project)

        def ghost(body: str) -> None:
            (inbox / "TODO-0035.md").write_text(body)
            (inbox / "TODO-0035.ready").touch()
            (outbox / "DONE-TODO-0035.md").write_text(body)

        ghost("first round")
        api.retire_todo(project, "TODO-0035", "first reject")
        api.restore_todo(project, "TODO-0035")
        ghost("second round")

        result = api.retire_todo(project, "TODO-0035", "second reject")

        done = paths.done_dir(project) / "TODO-0035"
        # latest task text wins the archive's TODO.md
        assert (done / "TODO.md").read_text() == "second round"
        # first round's files survived under suffixes, never overwritten
        assert (done / "DONE-TODO-0035.md").read_text() == "first round"
        assert (done / "DONE-TODO-0035-1.md").read_text() == "second round"
        assert (done / "TODO-0035.ready").is_file()
        assert (done / "TODO-0035-1.ready").is_file()
        # both RETIRED notes kept; the result points at the newest one
        assert len(list(done.glob("RETIRED-*.md"))) == 2
        assert "second reject" in (project / result.retired_note).read_text()
        # status clean after the second retire
        assert not (inbox / "TODO-0035.md").exists()
        assert "TODO-0035" not in todos.list_active_todos(inbox, outbox)

    def test_repeat_call_is_already_archived(self, project):
        self._ghost(project)
        api.retire_todo(project, "TODO-0035", "rejected at verify")

        with pytest.raises(api.AwfApiError, match="already archived"):
            api.retire_todo(project, "TODO-0035", "again")

    def test_missing_todo_is_error(self, project):
        with pytest.raises(api.AwfApiError, match="not found"):
            api.retire_todo(project, "TODO-0099", "gone")

    def test_already_archived_refused(self, project):
        (paths.done_dir(project) / "TODO-0035").mkdir(parents=True)
        (paths.done_dir(project) / "TODO-0035" / "TODO.md").write_text("old")

        with pytest.raises(api.AwfApiError, match="already archived"):
            api.retire_todo(project, "TODO-0035", "again")

    def test_already_archived_names_conflict_when_inbox_copy_exists(self, project):
        (paths.inbox(project) / "TODO-0035.md").write_text("re-issued?")
        (paths.done_dir(project) / "TODO-0035").mkdir(parents=True)
        (paths.done_dir(project) / "TODO-0035" / "TODO.md").write_text("old")

        with pytest.raises(api.AwfApiError, match="second copy"):
            api.retire_todo(project, "TODO-0035", "again")

    def test_empty_reason_refused_and_nothing_moves(self, project):
        self._ghost(project)
        for bad in ("", "   "):
            with pytest.raises(api.AwfApiError, match="reason is required"):
                api.retire_todo(project, "TODO-0035", bad)
        # untouched
        assert (paths.inbox(project) / "TODO-0035.md").is_file()
        assert not (paths.done_dir(project) / "TODO-0035").exists()

    def test_live_pipeline_on_this_id_refused(self, project, monkeypatch):
        self._ghost(project)
        self._write_state(project, "TODO-0035")
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda *a, **k: (True, 1234, "")
        )

        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.retire_todo(project, "TODO-0035", "rejected at verify")

        # nothing moved
        assert (paths.inbox(project) / "TODO-0035.md").is_file()
        assert not (paths.done_dir(project) / "TODO-0035").exists()

    def test_live_pipeline_on_other_id_allowed(self, project, monkeypatch):
        self._ghost(project)
        self._write_state(project, "TODO-0099")
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda *a, **k: (True, 1234, "")
        )

        result = api.retire_todo(project, "TODO-0035", "rejected at verify")

        assert result.todo_id == "TODO-0035"
        assert not (paths.inbox(project) / "TODO-0035.md").exists()

    def test_dead_pipeline_state_allows_retire(self, project):
        self._ghost(project)
        # stale state on this id but the PID is provably dead → not live
        from awf import pipeline_state

        pipeline_state.write_state(
            project, todo_id="TODO-0035", pipeline_pid=2**31 - 1
        )

        api.retire_todo(project, "TODO-0035", "rejected at verify")

        assert not (paths.inbox(project) / "TODO-0035.md").exists()

    def test_done_ready_signal_stays_in_outbox(self, project):
        """DONE-<id>.ready is a real closure signal — retire never moves it
        (spec: DONE-*.md/.json WITHOUT .ready)."""
        self._ghost(project)
        (paths.outbox(project) / "DONE-TODO-0035.ready").touch()

        api.retire_todo(project, "TODO-0035", "rejected at verify")

        assert (paths.outbox(project) / "DONE-TODO-0035.ready").is_file()
        assert not (paths.outbox(project) / "DONE-TODO-0035.md").exists()
        done = paths.done_dir(project) / "TODO-0035"
        assert not (done / "DONE-TODO-0035.ready").exists()

    def test_legacy_short_forms_moved(self, project):
        self._ghost(project, "TODO-0001")
        outbox = paths.outbox(project)
        (outbox / "PROGRESS-0001.md").write_text("legacy progress")
        (outbox / "DONE-0001.md").write_text("legacy done")
        (outbox / "REVIEW-0001.md").write_text("legacy review")

        api.retire_todo(project, "TODO-0001", "legacy ghost")

        assert not (outbox / "PROGRESS-0001.md").exists()
        assert not (outbox / "DONE-0001.md").exists()
        assert not (outbox / "REVIEW-0001.md").exists()
        done = paths.done_dir(project) / "TODO-0001"
        assert (done / "PROGRESS-0001.md").is_file()
        assert (done / "DONE-0001.md").is_file()
        assert (done / "REVIEW-0001.md").is_file()

    def test_does_not_touch_longer_id(self, project):
        """TODO-0001 vs TODO-00010: retiring 0001 must not move 00010's files."""
        self._ghost(project, "TODO-0001")
        inbox, outbox = paths.inbox(project), paths.outbox(project)
        (inbox / "TODO-00010.md").write_text("long id")
        (inbox / "TODO-00010.ready").touch()
        (outbox / "PROGRESS-TODO-00010.md").write_text("long id progress")
        (outbox / "DONE-TODO-00010.json").write_text("{}")

        api.retire_todo(project, "TODO-0001", "short ghost")

        assert (inbox / "TODO-00010.md").is_file()
        assert (inbox / "TODO-00010.ready").is_file()
        assert (outbox / "PROGRESS-TODO-00010.md").is_file()
        assert (outbox / "DONE-TODO-00010.json").is_file()

    def test_name_collision_gets_suffix(self, project):
        """A pre-existing file in done/<id>/ is never overwritten."""
        self._ghost(project, "TODO-0001")
        done = paths.done_dir(project) / "TODO-0001"
        done.mkdir(parents=True)
        (done / "PROGRESS-TODO-0001.md").write_text("old history")

        api.retire_todo(project, "TODO-0001", "second ghost")

        assert (done / "PROGRESS-TODO-0001.md").read_text() == "old history"
        assert (done / "PROGRESS-TODO-0001-1.md").is_file()
        assert (done / "TODO.md").is_file()

    def test_invalid_id_rejected(self, project):
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.retire_todo(project, "garbage", "reason")


# ─── D: update_todo (RUN6 #4 — reword a not-started TODO, keep the number)
#

class TestUpdateTodo:
    """``update_todo`` — reword the content in place; the number, the
    dispatch ``.ready`` and the baseline survive, the old content is
    backed up to ``context/TODO-<id>.md.bak-<ts>``.
    """

    def _write_baseline(self, project: Path, todo_id: str) -> None:
        (paths.context_dir(project) / f"BASELINE-{todo_id}.sha").write_text("a" * 40)

    def test_update_keeps_number_ready_baseline(self, project):
        _arm(project, "TODO-0001")
        self._write_baseline(project, "TODO-0001")
        old = (paths.inbox(project) / "TODO-0001.md").read_text()

        result = api.update_todo(project, "TODO-0001", "new text")

        inbox = paths.inbox(project)
        # number + dispatch shape + baseline untouched
        assert (inbox / "TODO-0001.md").is_file()
        assert (inbox / "TODO-0001.md").read_text() == "new text"
        assert (inbox / "TODO-0001.ready").is_file()
        assert (paths.context_dir(project) / "BASELINE-TODO-0001.sha").read_text() == "a" * 40
        assert result.todo_id == "TODO-0001"
        # TODO is still active under the same number
        assert "TODO-0001" in todos.list_active_todos(
            inbox, paths.outbox(project)
        )

    def test_backup_holds_old_content(self, project):
        _arm(project, "TODO-0001")
        old = (paths.inbox(project) / "TODO-0001.md").read_text()

        result = api.update_todo(project, "TODO-0001", "v2")

        backup = project / result.backup
        assert backup.is_file()
        assert backup.parent == paths.context_dir(project)
        assert backup.name.startswith("TODO-0001.md.bak-")
        assert backup.read_text() == old

    def test_second_update_keeps_both_backups(self, project):
        _arm(project, "TODO-0001")
        first = api.update_todo(project, "TODO-0001", "v2")
        second = api.update_todo(project, "TODO-0001", "v3")

        backups = sorted(paths.context_dir(project).glob("TODO-0001.md.bak-*"))
        assert len(backups) == 2
        assert {b.read_text() for b in backups} == {"task body", "v2"}
        assert first.backup != second.backup
        assert (paths.inbox(project) / "TODO-0001.md").read_text() == "v3"

    def test_update_without_ready_is_allowed(self, project):
        """A never-dispatched TODO (.md only) is not started — it may be
        reworded; there is no .ready to preserve."""
        (paths.inbox(project) / "TODO-0007.md").write_text("draft")

        result = api.update_todo(project, "TODO-0007", "draft v2")

        assert result.todo_id == "TODO-0007"
        assert (paths.inbox(project) / "TODO-0007.md").read_text() == "draft v2"
        assert not (paths.inbox(project) / "TODO-0007.ready").exists()

    def test_reason_goes_to_log(self, project):
        _arm(project, "TODO-0001")
        api.update_todo(project, "TODO-0001", "v2", reason="scope cut at plan")

        log = paths.logs_dir(project) / "orchestrator.log"
        text = log.read_text()
        assert "todo-update: TODO-0001" in text
        assert "scope cut at plan" in text

    def test_refuses_started_with_progress(self, project):
        _arm(project, "TODO-0002")
        (paths.outbox(project) / "PROGRESS-TODO-0002.md").write_text("half done")

        with pytest.raises(api.AwfApiError, match="in flight"):
            api.update_todo(project, "TODO-0002", "v2")

        # untouched, no backup
        assert (paths.inbox(project) / "TODO-0002.md").read_text() == "task body"
        assert not list(paths.context_dir(project).glob("TODO-0002.md.bak-*"))

    def test_refuses_started_with_outbox_signal(self, project):
        _arm(project, "TODO-0002")
        (paths.outbox(project) / "BLOCKED-TODO-0002.ready").touch()

        with pytest.raises(api.AwfApiError, match="in flight"):
            api.update_todo(project, "TODO-0002", "v2")

    def test_refuses_started_with_done_closure(self, project):
        _arm(project, "TODO-0002")
        (paths.outbox(project) / "DONE-TODO-0002.ready").touch()

        with pytest.raises(api.AwfApiError, match="in flight"):
            api.update_todo(project, "TODO-0002", "v2")

    def test_refuses_started_with_inbox_signal(self, project):
        _arm(project, "TODO-0002")
        (paths.inbox(project) / "APPROVE-TODO-0002.ready").touch()

        with pytest.raises(api.AwfApiError, match="in flight"):
            api.update_todo(project, "TODO-0002", "v2")

    def test_does_not_touch_longer_id(self, project):
        """TODO-0001 vs TODO-00010: rewording 0001 must not see 00010's
        outbox signals."""
        _arm(project, "TODO-0001")
        (paths.inbox(project) / "TODO-00010.md").write_text("long id")
        (paths.outbox(project) / "DONE-TODO-00010.ready").touch()

        result = api.update_todo(project, "TODO-0001", "v2")

        assert result.todo_id == "TODO-0001"
        assert (paths.inbox(project) / "TODO-00010.md").is_file()

    def test_missing_todo_is_error(self, project):
        with pytest.raises(api.AwfApiError, match="not found"):
            api.update_todo(project, "TODO-0099", "v2")

    def test_empty_content_refused(self, project):
        _arm(project, "TODO-0001")
        for bad in ("", "   \n"):
            with pytest.raises(api.AwfApiError, match="content is empty"):
                api.update_todo(project, "TODO-0001", bad)
        assert (paths.inbox(project) / "TODO-0001.md").read_text() == "task body"
        assert not list(paths.context_dir(project).glob("TODO-0001.md.bak-*"))

    def test_invalid_id_rejected(self, project):
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.update_todo(project, "garbage", "v2")

    def test_live_pipeline_on_this_id_refused(self, project, monkeypatch):
        _arm(project, "TODO-0001")
        from awf import pipeline_state

        pipeline_state.write_state(project, todo_id="TODO-0001", pipeline_pid=1)
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda *a, **k: (True, 1234, "")
        )

        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.update_todo(project, "TODO-0001", "v2")

        assert (paths.inbox(project) / "TODO-0001.md").read_text() == "task body"
        assert not list(paths.context_dir(project).glob("TODO-0001.md.bak-*"))

    def test_live_pipeline_on_other_id_allowed(self, project, monkeypatch):
        _arm(project, "TODO-0001")
        from awf import pipeline_state

        pipeline_state.write_state(project, todo_id="TODO-0099", pipeline_pid=1)
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda *a, **k: (True, 1234, "")
        )

        result = api.update_todo(project, "TODO-0001", "v2")

        assert result.todo_id == "TODO-0001"
        assert (paths.inbox(project) / "TODO-0001.md").read_text() == "v2"
