"""ORCH M3.4: awf_run_revise v1 — preview + pipeline change of the
NOT-STARTED elements of the active run's queue.

One typed API (``awf.api.run_revise`` + MCP tool) with:
- ``preview=True`` — no side effects: the current queue, the elements that
  would change, the conflicts (current/started/completed/not in queue), key;
- apply — changes the pipeline of the not-started elements only (positions
  >= index, never the current or completed element); a conflict in the
  request refuses the whole apply (atomic — the previous plan stays in
  force); a live engine (a stage is running) refuses with the stop-first
  hint (stopping is the next unit, ORCH M3.5);
- idempotency by ``key``: a repeat with the same key is a no-op (the stored
  revision is returned, nothing is written twice);
- the applied revision is recorded in the run state (state/run.yaml,
  ``revisions`` — the decisions' path) in the SAME CAS write as the queue
  change (A-13 generation condition).

The pipeline spawn is mocked (``awf.api.pipeline.start_pipeline``) —
hermetic. A "running engine" is faked at the shared liveness seam
(``awf.api._liveness.resolve``); a CAS miss is faked at
``run_state.update_run_cas``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import awf.api.pipeline as api_pipeline
from awf import api, run_state

T1, T2, T3 = "TODO-0001", "TODO-0002", "TODO-0003"
ALT = "alt"


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


def _pipelines(state: dict) -> dict[str, str]:
    return {
        str(q.get("todo_id", "")): str(q.get("pipeline", "") or "")
        for q in state.get("queue") or []
        if isinstance(q, dict)
    }


# ─── prove_red: idempotency by key ───────────────────────────────────────


def test_revise_is_idempotent_by_key(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) a repeat with the same key is a no-op: no second queue change,
    no second revision record. Red before the fix: api.run_revise does not
    exist (AttributeError)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    first = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key="k1"
    )
    assert first.action == "applied"
    state = run_state.read_run(proj)
    assert _pipelines(state)[T2] == ALT
    assert len(state.get("revisions") or []) == 1

    # the repeat: same key, a DIFFERENT queue — it must not re-apply
    again = api.run_revise(
        proj, queue=[{"todo_id": T3, "pipeline": "other"}], reason="why", key="k1"
    )
    assert again.action == "noop"
    assert again.revision is not None and again.revision["key"] == "k1"
    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: ALT, T3: ""}, (
        "the repeat must not touch the queue"
    )
    assert len(state.get("revisions") or []) == 1, "no second revision record"


# ─── prove_red: refused while a stage is running ─────────────────────────


def test_revise_refused_while_stage_running(tmp_git_repo: Path, monkeypatch) -> None:
    """(2) a live engine (a stage is running) — the apply is refused with
    the stop-first hint; the queue and the revision memory are untouched.
    The preview stays available (it is read-only). Red before the fix:
    api.run_revise does not exist (AttributeError)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert "stop the unit first" in result.message
    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}, (
        "the refused apply must not change the queue"
    )
    assert not state.get("revisions"), "the refused apply must not record a revision"

    # the preview is read-only — it is not refused by the live engine
    preview = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}],
        reason="why", key="k1", preview=True,
    )
    assert preview.action == "preview"
    assert [c["todo_id"] for c in preview.changes] == [T2]


# ─── preview semantics ───────────────────────────────────────────────────


def test_preview_has_no_side_effects(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) preview=true: the current queue (with per-item lifecycle state),
    the elements that would change, conflicts — and nothing is written."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": T2, "pipeline": ALT}, {"todo_id": T3, "pipeline": "alt2"}],
        reason="why", key="k1", preview=True,
    )
    assert result.action == "preview" and result.preview is True
    assert result.key == "k1"
    assert [c["todo_id"] for c in result.changes] == [T2, T3]
    assert all(c["from"] == "" for c in result.changes)
    assert result.conflicts == []
    states = {e["todo_id"]: e["state"] for e in result.current_queue}
    assert states == {T1: "current", T2: "queued", T3: "queued"}

    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}
    assert not state.get("revisions")


# ─── apply semantics ─────────────────────────────────────────────────────


def test_apply_changes_not_started_elements_only(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) the apply changes the pipeline of the not-started elements only;
    the current element, the run position and the completed list are
    untouched."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": T2, "pipeline": ALT}, {"todo_id": T3, "pipeline": "alt2"}],
        reason="why", key="k1",
    )
    assert result.action == "applied"
    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: ALT, T3: "alt2"}
    assert state["index"] == 1 and state["current"] == T1
    assert list(state.get("completed") or []) == []


def test_apply_refused_when_current_element_requested(tmp_git_repo: Path, monkeypatch) -> None:
    """The current (in-flight) element is a conflict — the whole apply is
    refused (atomic): even the valid part of the request is not applied."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": T1, "pipeline": ALT}, {"todo_id": T2, "pipeline": ALT}],
        reason="why", key="k1",
    )
    assert result.action == "refused"
    assert result.conflicts and result.conflicts[0]["todo_id"] == T1
    assert "current" in result.conflicts[0]["reason"]
    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}, (
        "a conflicted apply must change nothing"
    )
    assert not state.get("revisions")


def test_apply_refused_when_todo_not_in_queue(tmp_git_repo: Path, monkeypatch) -> None:
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": "TODO-0099", "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert result.conflicts[0]["reason"] == "not in the run queue"
    assert not run_state.read_run(proj).get("revisions")


def test_apply_refused_when_completed_requested(tmp_git_repo: Path, monkeypatch) -> None:
    """A completed element is never revised (hand-written run.yaml: index=2,
    current=T2, completed=[T1] — T1 at a not-started position would not
    prove the completed check)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _raw_write(proj, {
        "active": True,
        "queue": [{"todo_id": t, "pipeline": ""} for t in (T1, T2, T3)],
        "index": 2, "current": T2, "completed": [T1],
        "generation": 1, "started_at": run_state.now_iso(),
    })

    result = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert result.conflicts[0]["reason"] == "already completed"


def test_apply_requires_a_key(tmp_git_repo: Path, monkeypatch) -> None:
    """The key is the idempotency identity — the apply without it is
    refused (a preview without a key is fine)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key=""
    )
    assert result.action == "refused"
    assert "key" in result.message
    assert not run_state.read_run(proj).get("revisions")


def test_refused_without_active_run(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    result = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert "No active run" in result.message


def test_cas_mismatch_refuses_and_keeps_the_plan(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) A-13: the run changed between the read and the write — the CAS
    tuple no longer matches, nothing is written, the refusal carries the
    current state and the previous plan stays in force."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    monkeypatch.setattr(run_state, "update_run_cas", lambda *a, **k: (None, False))

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert "CAS" in result.message
    assert "stop the unit" not in result.message
    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}
    assert not state.get("revisions")


# ─── the revision record (decisions' path) ───────────────────────────────


def test_applied_revision_is_recorded_in_run_state(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) the applied revision lands in state/run.yaml — ts /
    kind=revision / reason / changes (the decisions' style), with the key
    and the generation at apply time."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key="k1"
    )
    entry = run_state.read_run(proj)["revisions"][0]
    assert entry["kind"] == "revision"
    assert entry["key"] == "k1"
    assert entry["reason"] == "why"
    assert entry["ts"]
    assert entry["changes"] == [{"todo_id": T2, "from": "", "to": ALT}]
    assert entry["generation"] == 1


def test_apply_without_effective_change_records_nothing(tmp_git_repo: Path, monkeypatch) -> None:
    """A request whose pipeline already matches the queue changes nothing —
    no revision record (there is nothing to revise)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ""}], reason="why", key="k1"
    )
    assert result.action == "noop"
    assert result.changes == []
    assert not run_state.read_run(proj).get("revisions")


def test_corrupt_revisions_degrade_on_read(tmp_git_repo: Path, monkeypatch) -> None:
    """A broken ``revisions`` value in run.yaml degrades to [] with the rest
    of the run usable (the decisions' degradation class); a partially
    broken entry drops, the valid part survives."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    base = run_state.read_run(proj)
    base["revisions"] = "garbage"
    _raw_write(proj, base)
    state = run_state.read_run(proj)
    assert state is not None and state["revisions"] == []

    base2 = run_state.read_run(proj)
    base2["revisions"] = [
        42,
        {"key": "k9", "reason": "r",
         "changes": [{"todo_id": T3, "from": "", "to": ALT}, {"bad": 1}]},
    ]
    _raw_write(proj, base2)
    state2 = run_state.read_run(proj)
    assert len(state2["revisions"]) == 1
    entry = state2["revisions"][0]
    assert entry["key"] == "k9" and entry["kind"] == "revision"
    assert entry["changes"] == [{"todo_id": T3, "from": "", "to": ALT}]
