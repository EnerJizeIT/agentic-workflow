"""AUD05-05 (остаток): caller-level согласованность run.yaml.

The internal write_run lock (FU-09 first pass) serializes the write itself,
but the CALLERS computed whole keys (outcomes / rejects / index / current)
from a snapshot read BEFORE the lock — the shallow merge then clobbered a
concurrent writer's update. Acceptance criteria from the audit report
(~/Desktop/awf-audit/reports/AUD-05-api-verbs.md, AUD05-05) + the supervisor
repro (REVIEW-TODO-0006: 17/30 inconsistent):

- in ANY interleaving of parallel approve_commit + reject_commit the final
  run.yaml is internally consistent: verdict=='approved' ⇒ rejects == 0;
  a reject is never lost;
- run_next + run_finish racing: if the run closed during the launch window
  the item is NOT recorded as current (an orphaned pipeline is the accepted
  consequence); the combination active=False + current=<launched TODO> with
  an unadvanced index is not reachable.

On the pre-fix code these tests are RED — that is the point.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state

TODO = "TODO-0001"
ITERS = 30


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="AUD05Rest")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = TODO, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _reset_diary(proj: Path) -> None:
    """Fresh active run state for the next race iteration (test harness,
    not code under test): the run stays active, counters start clean."""
    run_state.write_run(proj, rejects={}, outcomes={}, index=0, current="")


class TestApproveRejectRace:
    """Supervisor repro: barrier → parallel approve_commit + reject_commit
    on one TODO, 30 iterations. After each iteration the diary must agree
    with the counter: 'approved' ⇒ rejects == 0, and the single reject of
    the iteration is never lost."""

    def test_parallel_approve_reject_stays_consistent(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _write_todo(proj)
        api.run_start(proj, queue=[TODO])

        errors: list = []
        violations: list = []

        for i in range(ITERS):
            _reset_diary(proj)
            barrier = threading.Barrier(2)

            def approve():
                try:
                    barrier.wait(timeout=10)
                    api.approve_commit(proj, TODO, evidence=f"iter {i}: probes ok")
                except Exception as e:  # noqa: BLE001 — surface in the test
                    errors.append(f"approve iter {i}: {e!r}")

            def reject():
                try:
                    barrier.wait(timeout=10)
                    api.reject_commit(proj, TODO, f"iter {i}: broken")
                except Exception as e:  # noqa: BLE001 — surface in the test
                    errors.append(f"reject iter {i}: {e!r}")

            t1 = threading.Thread(target=approve)
            t2 = threading.Thread(target=reject)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

            state = run_state.read_run(proj) or {}
            rejects_n = int((state.get("rejects") or {}).get(TODO, 0) or 0)
            entry = (state.get("outcomes") or {}).get(TODO)

            # (a) the reject of this iteration is never lost
            if rejects_n != 1:
                violations.append(f"iter {i}: reject lost — rejects[TODO]={rejects_n}")
            # (b) audit invariant: 'approved' ⇒ rejects == 0
            if isinstance(entry, dict) and entry.get("verdict") == "approved" and rejects_n != 0:
                violations.append(
                    f"iter {i}: verdict='approved' while rejects[TODO]={rejects_n} — "
                    "diary disagrees with the counter"
                )

        assert not errors, errors
        assert not violations, (
            f"{len(violations)}/{ITERS} iterations left run.yaml inconsistent:\n"
            + "\n".join(violations)
        )


class TestRunNextRunFinishWindow:
    """run_next + run_finish racing: the launch is faked as slow (0.5s),
    run_finish lands inside the launch window. The item must NOT be
    recorded as current of a closed run — either it is not recorded at all
    (deterministic with the fake: finish wins the race), or the run is
    still active. active=False + current=<launched TODO> with an
    unadvanced index is the forbidden combination (orphan no one owns)."""

    def test_finish_in_launch_window_does_not_record_item(self, tmp_git_repo, monkeypatch):
        proj = _project(tmp_git_repo)
        _write_todo(proj)
        api.run_start(proj, queue=[TODO])

        def slow_bg(project_dir, **kw):
            time.sleep(0.5)  # fake slow launch
            return 424242, proj / ".agentic" / "logs" / "awf-start.out", None

        monkeypatch.setattr(api_pipeline, "start_in_background", slow_bg)
        monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda *a, **kw: True)

        box: dict = {}

        def launch():
            box["result"] = api.run_next(proj)

        t = threading.Thread(target=launch)
        t.start()
        time.sleep(0.25)  # inside the fake launch window
        api.run_finish(proj, reason="owner stop")
        t.join(timeout=15)
        assert not t.is_alive(), "run_next hung"

        state = run_state.read_run(proj) or {}
        # the general criterion: a closed run cannot own a current item
        if state.get("current") == TODO:
            assert state.get("active") is True, (
                f"forbidden combination: active={state.get('active')}, "
                f"current={state.get('current')}, index={state.get('index')}"
            )
        # deterministic with the fake: finish landed before the advance →
        # index/current were NOT committed
        assert state.get("active") is False
        assert state.get("index") == 0, f"index advanced to {state.get('index')}"
        assert not state.get("current"), f"current set to {state.get('current')!r}"

        # the result must tell the supervisor the pipeline is orphaned
        result = box["result"]
        assert result.todo_id == TODO
        assert result.action == "started"
        text = (result.message + " " + result.next_action).lower()
        assert "closed" in text or "orphan" in text, (
            f"result does not report the closed-run race: {result.message!r}"
        )


class TestUpdateRunAtomic:
    """A: update_run — read → mutate → write in one lock hold. The mutator
    must see the state AS IT IS ON DISK at that moment (not a stale
    snapshot), and the written state must be exactly what it returned."""

    def test_mutator_sees_fresh_state(self, tmp_git_repo):
        proj = tmp_git_repo
        run_state.write_run(proj, active=True, queue=[TODO], rejects={TODO: 0}, outcomes={})

        seen = {}

        def mutator(state: dict) -> dict:
            seen["rejects"] = dict(state.get("rejects") or {})
            state["rejects"] = {TODO: seen["rejects"].get(TODO, 0) + 1}
            return state

        final = run_state.update_run(proj, mutator)

        assert seen["rejects"] == {TODO: 0}, "mutator did not see the on-disk state"
        assert final["rejects"] == {TODO: 1}
        assert run_state.read_run(proj)["rejects"] == {TODO: 1}

    def test_mutator_veto_leaves_diary_untouched(self, tmp_git_repo):
        """The approve-conflict path: mutator returns the input unchanged —
        no 'approved' verdict may appear while a reject is recorded."""
        proj = tmp_git_repo
        run_state.write_run(
            proj, active=True, queue=[TODO],
            rejects={TODO: 1}, outcomes={TODO: {"verdict": "rejected", "rejects": 1}},
        )

        def mutator(state: dict) -> dict:
            if int((state.get("rejects") or {}).get(TODO, 0) or 0) >= 1:
                return state  # conflict — veto
            state["outcomes"] = {TODO: {"verdict": "approved"}}
            return state

        run_state.update_run(proj, mutator)

        state = run_state.read_run(proj)
        assert state["outcomes"][TODO]["verdict"] == "rejected"
        assert state["rejects"] == {TODO: 1}
