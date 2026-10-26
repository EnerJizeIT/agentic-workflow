"""RB-3 audit fixes: the retry target must be a FRESH id (F3) and the
internal ``retry`` marker must never leak into ANY answer, including an
early refusal (F4).

Helpers are shared with ``test_run_revise_retry.py`` (same fixtures).
"""
from __future__ import annotations

from pathlib import Path

from awf import api, run_state
from tests.negative.test_run_revise_retry import (
    RETRY,
    T1,
    T2,
    _launch_first,
    _project,
    _raw_write,
    _reject_current,
    _write_todo,
)


def test_retry_target_with_a_counted_reject_is_refused(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(F3) A target id that already carries a reject can never be approved
    (`approved ⇒ rejects == 0`); the retry refuses it and names a fresh id,
    with no side effects on the queue, position or revisions."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    state = run_state.read_run(proj)
    state["rejects"] = {T1: 1, RETRY: 1}
    _raw_write(proj, state)

    result = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry onto an already-rejected id",
        key="retry-rejected-target",
    )
    assert result.action == "refused"
    assert any("issue a fresh TODO id" in c["reason"] for c in result.conflicts)
    state = run_state.read_run(proj)
    assert [q["todo_id"] for q in state["queue"]] == [T1, T2]
    assert state["index"] == 1 and state["current"] == T1
    assert not state.get("revisions")


def test_retry_marker_is_stripped_from_an_early_refusal(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(F4) The internal ``retry`` marker is stripped BEFORE every early
    return: an invalid position refuses with ``changes`` carrying the
    documented ``{todo_id, from, to, class}`` shape only."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY, "TODO-0006"):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    result = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}, {"todo_id": "TODO-0006"}],
        reason="retry plus an invalid insert",
        key="retry-bad-position",
        position=0,
    )
    assert result.action == "refused"
    assert "invalid position" in result.message
    replaced = [c for c in result.changes if c.get("class") == "replaced"]
    assert replaced, "the retry change must be visible in the refusal"
    for change in result.changes:
        assert "retry" not in change
    assert replaced[0] == {
        "todo_id": RETRY,
        "from": T1,
        "to": RETRY,
        "class": "replaced",
    }
