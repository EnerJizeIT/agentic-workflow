"""RB-3: awf_run_revise retry replace — reject → replace the CURRENT
rejected element with a fresh retry TODO.

The retry keeps the element's slot (queue length and position), the run
stays active, the reject counter/verdict stay, and ``run_next`` launches
the retry there. The path works only on a dead stage; a live/approved/
completed current is a conflict. See ``test_run_revise.py`` for the core
revise contract (preview, key idempotency, not-started replace).

The pipeline spawn is mocked (``awf.api.pipeline.start_pipeline``) —
hermetic; the "running engine" is faked at the shared liveness seam
(``awf.api._liveness.resolve``).
"""
from __future__ import annotations

from pathlib import Path

import yaml

import awf.api.pipeline as api_pipeline
from awf import api, run_state

T1, T2, T3 = "TODO-0001", "TODO-0002", "TODO-0003"
RETRY, RETRY2 = "TODO-0004", "TODO-0005"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="RunRevise")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\n", encoding="utf-8")


def _fake_start(monkeypatch, proj: Path) -> None:
    def fake_start(project_dir, **kw):
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _launch_first(proj: Path, monkeypatch, queue: list[str]) -> None:
    """run_start + one launch: index=1, current=T1, T2/T3 not started."""
    api.run_start(proj, queue=queue)
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"


def _raw_write(proj: Path, state: dict) -> None:
    """Write run.yaml exactly as given — an on-disk state the sanitized
    reader must survive (the write path cannot produce corruption)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")




def _reject_current(proj: Path, todo_id: str = T1) -> None:
    result = api.reject_commit(proj, todo_id, "the plan is wrong — retry it")
    assert result.rejects == 1


def test_retry_replace_of_the_rejected_current_launches_the_retry(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.1/4) A rejected current element is replaced in place with a
    retry TODO: the position is kept, the run stays active, ``run_next``
    launches the retry next and the rest of the queue follows it."""
    from awf.todos import archive_todo

    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    _reject_current(proj)

    applied = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry the unit with a fixed plan",
        key="retry-1",
    )
    assert applied.action == "applied", applied.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [RETRY, T2, T3]
    assert state["index"] == 0 and state["current"] == "", (
        "the retry takes the rejected element's slot and is the next to launch"
    )

    launched = api.run_next(proj)
    assert launched.action == "started", launched.message
    assert launched.todo_id == RETRY
    state = run_state.read_run(proj)
    assert state["current"] == RETRY and state["index"] == 1

    status = api.run_status(proj)
    assert status.current == RETRY
    assert status.rejects == {T1: 1}, "the reject history survives the retry"
    assert [q["todo_id"] for q in status.queue] == [RETRY, T2, T3]

    # The rest of the queue follows the retry: finish it, launch the next.
    (proj / ".agentic" / "inbox" / f"{RETRY}.ready").write_text("", encoding="utf-8")
    archive_todo(proj, RETRY)
    nxt = api.run_next(proj)
    assert nxt.action == "started", nxt.message
    assert nxt.todo_id == T2


def test_retry_replace_refused_while_the_stage_is_live(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.2) The retry replace applies only to a dead stage: a live
    current element is refused with text, even with stop_running=true, and
    the standard kill path is never entered."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )

    def _no_kill(*a, **kw):
        raise AssertionError("the retry replace must not kill a live stage")

    monkeypatch.setattr(api_pipeline, "kill_pipeline", _no_kill)

    result = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-live",
        stop_running=True,
    )
    assert result.action == "refused"
    assert "running" in result.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [T1, T2]
    assert state["index"] == 1 and state["current"] == T1
    assert not state.get("revisions")


def test_retry_replace_refused_for_an_approved_current(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.2) An approved (and then finished) current element is not a
    retry candidate — the refusal names the state and changes nothing."""
    from awf.todos import archive_todo

    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])

    state = run_state.read_run(proj)
    state["outcomes"] = {T1: {"verdict": "approved"}}
    state["rejects"] = {}
    _raw_write(proj, state)

    approved = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-approved",
    )
    assert approved.action == "refused"
    assert any("approved" in c["reason"] for c in approved.conflicts)
    assert not run_state.read_run(proj).get("revisions")

    archive_todo(proj, T1)
    finished = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-finished",
    )
    assert finished.action == "refused"
    assert any("completed" in c["reason"] for c in finished.conflicts)


def test_retry_replace_refused_without_a_launchable_todo(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.3) The retry needs a launchable TODO file; a missing one is a
    refusal with no side effects (queue, position and revision memory)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    result = api.run_revise(
        proj,
        queue=[{"from": T1, "to": "TODO-0099"}],
        reason="retry",
        key="retry-missing",
    )
    assert result.action == "refused"
    assert any("TODO file" in c["reason"] for c in result.conflicts)
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [T1, T2]
    assert state["index"] == 1 and state["current"] == T1
    assert not state.get("revisions")


def test_retry_replace_keeps_the_reject_history_and_not_started_replace_works(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.5) The reject counter and verdict stay as they were, the
    revision is recorded, and a plain not-started replace still applies."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, RETRY, RETRY2):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    _reject_current(proj)

    applied = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry the unit",
        key="retry-keep",
    )
    assert applied.action == "applied", applied.message
    state = run_state.read_run(proj)
    assert state["rejects"] == {T1: 1}
    assert state["outcomes"][T1]["verdict"] == "rejected"
    assert state["outcomes"][T1]["reason"] == "the plan is wrong — retry it"
    entry = state["revisions"][0]
    assert entry["changes"] == [
        {"todo_id": RETRY, "from": T1, "to": RETRY, "class": "replaced"}
    ]
    assert entry["generation"] == 1

    not_started = api.run_revise(
        proj,
        queue=[{"from": T2, "to": RETRY2}],
        reason="swap a queued element",
        key="replace-queued",
    )
    assert not_started.action == "applied", not_started.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [RETRY, RETRY2, T3]
    assert state["index"] == 0 and state["current"] == ""


def test_retry_replace_of_a_middle_element_keeps_its_slot_and_the_rest_follows(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.1/4) The rejected element does not have to be the first or the
    last: a middle element gives its slot to the retry, the run position
    moves back to that slot, and the queue continues after the retry."""
    from awf.todos import archive_todo

    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    archive_todo(proj, T1)
    assert api.run_next(proj).todo_id == T2
    _reject_current(proj, T2)

    applied = api.run_revise(
        proj,
        queue=[{"from": T2, "to": RETRY}],
        reason="retry the middle unit",
        key="retry-middle",
    )
    assert applied.action == "applied", applied.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [T1, RETRY, T3]
    assert state["index"] == 1 and state["current"] == "", (
        "the retry stands in the rejected element's slot, not at the queue end"
    )
    assert state["completed"] == [T1]
    assert state["rejects"] == {T2: 1}

    assert api.run_next(proj).todo_id == RETRY
    (proj / ".agentic" / "inbox" / f"{RETRY}.ready").write_text("", encoding="utf-8")
    archive_todo(proj, RETRY)
    assert api.run_next(proj).todo_id == T3, "the queue after the retry still runs"


def test_retry_replace_of_the_last_element_reopens_an_exhausted_slot(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.1/4) The rejected LAST element sits at ``index == len(queue)``;
    the retry replace moves the position back so ``run_next`` launches it
    instead of stopping on "queue exhausted"."""
    proj = _project(tmp_git_repo)
    for t in (T1, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1])
    state = run_state.read_run(proj)
    assert state["index"] == 1 == len(state["queue"])
    _reject_current(proj)

    applied = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry the last unit",
        key="retry-last",
    )
    assert applied.action == "applied", applied.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [RETRY]
    assert state["index"] == 0 and state["current"] == ""

    launched = api.run_next(proj)
    assert launched.action == "started", launched.message
    assert launched.todo_id == RETRY


def test_retry_replace_replay_is_a_noop_and_keeps_the_position(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(RB-3.1) The retry replace is idempotent by key: a replay before the
    launch records no second revision and does not move the position again."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    first = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-replay",
    )
    assert first.action == "applied", first.message
    replay = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-replay",
    )
    assert replay.action == "noop", replay.message
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [RETRY, T2]
    assert state["index"] == 0 and state["current"] == ""
    assert len(state.get("revisions") or []) == 1


def test_reject_hint_falls_back_after_the_retry_replaced_the_element() -> None:
    """(design review) Once the retry took the rejected element's slot, the
    run card must stop repeating the stale "replace it" command — that
    request would now be refused ("source TODO is not in the run queue").
    Before the replace the hint names the rejected id and awf_run_revise."""
    from awf.run_plan_read import RunRecord, next_action_for_record

    rejected = {"kind": "reject", "todo_id": T1}
    before = RunRecord(
        active=True,
        last_decision=rejected,
        state={"queue": [{"todo_id": T1, "pipeline": ""}, {"todo_id": T2, "pipeline": ""}]},
    )
    hint = next_action_for_record(before, "FALLBACK")
    assert "awf_run_revise" in hint and T1 in hint

    after = RunRecord(
        active=True,
        last_decision=rejected,
        state={"queue": [{"todo_id": RETRY, "pipeline": ""}, {"todo_id": T2, "pipeline": ""}]},
    )
    assert next_action_for_record(after, "FALLBACK") == "FALLBACK"
