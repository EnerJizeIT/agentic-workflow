"""RB-3 contract: the recovery hints name the retry replace FIRST (F2) and
the applied retry tells the supervisor to retire only AFTER the retry (F5).

Both surfaces are what a fresh session reads right after a reject: the
``awf_run_next`` refusal and the ``run_revise`` applied answer. The order
matters — a retire first marks the element completed and the retry replace
then refuses it ("already completed").
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from tests.negative.test_run_revise_retry import (
    RETRY,
    T1,
    T2,
    _launch_first,
    _project,
    _reject_current,
    _write_todo,
)


def test_run_next_reject_hint_names_the_retry_before_the_retire(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(F2) The gate that refuses the next launch over an open reject names
    the retry replace — and names it BEFORE the retire, because the
    opposite order closes the retry slot."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    refused = api.run_next(proj)
    assert refused.action == "refused"
    hint = refused.next_action
    assert "awf_run_revise" in hint and f'"from": "{T1}"' in hint
    assert "awf_todo_retire" in hint
    assert hint.index("awf_run_revise") < hint.index("awf_todo_retire"), (
        "the retry replace must come before the retire"
    )


def test_applied_retry_tells_to_retire_only_after_the_retry(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(F5) The applied answer names awf_run_next as the next step and the
    retire as hygiene AFTER the retry — the revision never retires by
    itself, and the answer carries the documented change shape."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, RETRY):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])
    _reject_current(proj)

    applied = api.run_revise(
        proj,
        queue=[{"from": T1, "to": RETRY}],
        reason="retry",
        key="retry-hint",
    )
    assert applied.action == "applied", applied.message
    hint = applied.next_action
    assert "awf_run_next" in hint and "AFTER the retry" in hint
    assert "awf_todo_retire" in hint
    assert hint.index("awf_run_next") < hint.index("awf_todo_retire")
    assert applied.changes == [
        {"todo_id": RETRY, "from": T1, "to": RETRY, "class": "replaced"}
    ], "the internal retry marker must never leak into the answer"
