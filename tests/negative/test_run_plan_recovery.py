"""ORCH M1.3: supervisor context recovery — the §1.4 scenarios.

A fresh supervisor session (context lost) must restore from FILES alone:
the run goal, the position, the last decision and the permitted next
action — on an active run, after a reject, and after a done TODO before
the next `run_next` (IMPLEMENTATION-STRATEGY §1.4, acceptance: the new
session does not guess and does not launch over an unreviewed result).

Hermetic: a synthetic git project, real public API calls (run_start /
run_next / reject_commit / approve_commit / retire_todo / brief /
load_supervisor_context / run_status); the pipeline spawn is mocked
(`awf.api.pipeline.start_pipeline`) — no live processes.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from awf import api, git_utils, run_state

T = "TODO-0001"
T2 = "TODO-0002"
HIST = "TODO-0000"  # seeded archive — makes the project "live" (RUN3-7)


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Recovery")
    return tmp_git_repo


def _make_live(proj: Path) -> None:
    """A configured AND run project (RUN3-7): a minimal pipeline file plus
    one archived TODO — the card renders the working cycle, not the setup
    chain, so the run-aware next action is visible in the text."""
    pipes = proj / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "name: default\nstages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: worker\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    done = proj / ".agentic" / "done" / HIST
    done.mkdir(parents=True, exist_ok=True)
    (done / "TODO.md").write_text(f"# {HIST}\n", encoding="utf-8")


def _write_todo(proj: Path, todo_id: str, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _raw_write(proj: Path, state: object, raw: str | None = None) -> None:
    """Write run.yaml exactly as given — simulates an on-disk corruption
    the sanitized reader must survive (the normal write path cannot
    produce it)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        raw if raw is not None else yaml.safe_dump(state, allow_unicode=True),
        encoding="utf-8",
    )


def _fake_start(monkeypatch, proj: Path, *, boom: bool = False) -> dict:
    """Mock the pipeline spawn — the scenarios stay hermetic. ``boom``
    raises instead of faking a launch: a gate test must never reach it."""
    import awf.api.pipeline as api_pipeline

    captured: dict = {"calls": 0}

    def fake_start(project_dir, **kw):
        captured["calls"] += 1
        if boom:
            raise AssertionError("start_pipeline was called — the launch must not happen")
        captured.update(kw)
        return api_pipeline.StartResult(
            run_mode="background",
            run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0,
            message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    return captured


class TestNewSessionRecovery:
    """Scenario 1: an active run, "new session" — a fresh read of the state
    (brief / load_supervisor_context / run_status) restores the goal, the
    position, the last decision and the permitted action, as if the context
    were lost and rebuilt from the files."""

    def test_fresh_read_restores_goal_position_decision_and_action(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = _project(tmp_git_repo)
        _make_live(proj)
        _write_todo(proj, T)
        _write_todo(proj, T2)
        api.run_start(
            proj,
            queue=[T, T2],
            goal="Stabilize the run state",
            criteria=["gates green"],
        )
        _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"  # launch T
        api.reject_commit(proj, T, "diff too wide")

        # "New session": nothing in memory — only fresh public reads.
        st = api.run_status(proj)
        assert st.active is True
        assert st.goal == "Stabilize the run state"
        assert st.position == "1/2"
        assert st.current == T
        assert st.decisions[-1]["kind"] == "reject"
        assert st.decisions[-1]["todo_id"] == T
        assert st.decisions[-1]["reason"] == "diff too wide"

        b = api.brief(proj)
        assert b.run_goal == "Stabilize the run state"
        assert b.run_last_decision == f"reject {T} — diff too wide"
        assert "awf_run_next" in b.next_action
        assert "1/2" in b.text  # the position is on the card, not just a field
        assert "awf_run_next" in b.text  # and the permitted action is rendered

        c = api.load_supervisor_context(proj)
        # the two card views are ONE record
        assert c.run_goal == b.run_goal
        assert c.run_position == st.position
        assert c.run_last_decision == b.run_last_decision
        assert c.run_sources == b.run_sources
        assert "awf_run_next" in c.next_action

    def test_fresh_read_before_any_decision(self, tmp_git_repo: Path, monkeypatch) -> None:
        """Right after the first launch, no decisions yet: the goal, the
        position and the current item are still restored; the fresh session
        is never left without a next step."""
        proj = _project(tmp_git_repo)
        _make_live(proj)
        _write_todo(proj, T)
        api.run_start(proj, queue=[T], goal="Ship the first increment")
        _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"

        st = api.run_status(proj)
        assert st.goal == "Ship the first increment"
        assert st.position == "1/1"
        assert st.current == T
        assert st.decisions == []

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_goal == c.run_goal == "Ship the first increment"
        assert b.run_last_decision == ""
        assert b.next_action  # every answer leads to the next step
        assert c.run_active is True


class TestAfterRejectGate:
    """Scenario 2: after a reject — the next `run_next` does NOT launch the
    next queue item while the decision on the rejected TODO is open
    (unreviewed result). The launch unblocks only when the decision is
    closed (replan = new TODO + retire of the old one; approve of a fresh
    attempt; or the owner stops the run)."""

    def test_run_next_refuses_over_open_reject(self, tmp_git_repo: Path, monkeypatch) -> None:
        proj = _project(tmp_git_repo)
        _write_todo(proj, T)
        _write_todo(proj, T2)
        api.run_start(proj, queue=[T, T2])
        captured = _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"  # launch T

        api.reject_commit(proj, T, "missing regression test")

        result = api.run_next(proj)

        assert result.action == "refused"
        assert captured["calls"] == 1  # no second launch
        # the refusal names the open decision and how to close it — a fresh
        # session must not be told to "approve/reject" again (the approve is
        # refused against a counted reject; a second reject stops the run)
        assert f"{T} was rejected" in result.message
        assert f"REVIEW-{T}" in result.next_action
        assert "awf_todo_retire" in result.next_action
        state = run_state.read_run(proj)
        assert (state["index"], state["current"]) == (1, T)  # position untouched

    def test_launch_unblocks_after_retire(self, tmp_git_repo: Path, monkeypatch) -> None:
        """Retiring the rejected TODO closes the decision (the replan path
        ends in the same archive) — the next queue item launches."""
        proj = _project(tmp_git_repo)
        _write_todo(proj, T)
        _write_todo(proj, T2)
        api.run_start(proj, queue=[T, T2])
        captured = _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"  # launch T
        api.reject_commit(proj, T, "missing regression test")

        api.retire_todo(proj, T, "replanned as a new TODO")
        assert (proj / ".agentic" / "done" / T).is_dir()  # archived = closed

        result = api.run_next(proj)

        assert result.action == "started"
        assert result.todo_id == T2
        assert captured.get("todo_id") == T2
        assert run_state.read_run(proj)["index"] == 2


class TestAfterDoneNextStep:
    """Scenario 3: after a done (approved + archived) TODO, before the next
    `run_next`: the card shows the next step as `awf_run_next` (the run
    owns the queue position — not the generic "plan the next TODO"), the
    goal is not lost, the position is correct."""

    @staticmethod
    def _done(proj: Path, monkeypatch, queue: list[str]) -> None:
        from awf.todos import archive_todo

        for tid in queue:
            _write_todo(proj, tid)
        api.run_start(proj, queue=queue, goal="Ship the increment")
        _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"
        fp = git_utils.tree_fingerprint(proj)
        api.approve_commit(
            proj,
            queue[0],
            evidence="pytest -q → 12 passed; ruff → clean; verdict: approve",
            verified_sha=fp,
        )
        archive_todo(proj, queue[0])  # the commit gate's archive

    def test_brief_points_to_run_next(self, tmp_git_repo: Path, monkeypatch) -> None:
        proj = _project(tmp_git_repo)
        _make_live(proj)
        self._done(proj, monkeypatch, [T, T2])

        b = api.brief(proj)
        assert b.run_goal == "Ship the increment"  # the goal survived
        assert b.run_last_decision.startswith(f"approve {T}")
        assert "awf_run_next" in b.next_action
        assert T2 in b.next_action  # names the next queue item
        assert "awf_dispatch_todo" not in b.next_action
        assert "awf_run_next" in b.text  # rendered, not just a field

    def test_context_agrees_and_position_is_correct(self, tmp_git_repo: Path, monkeypatch) -> None:
        proj = _project(tmp_git_repo)
        _make_live(proj)
        self._done(proj, monkeypatch, [T, T2])

        c = api.load_supervisor_context(proj)
        st = api.run_status(proj)
        assert c.run_goal == "Ship the increment"
        assert c.run_position == st.position == "1/2"  # item 1 of 2 was the run slot
        assert "awf_run_next" in c.next_action
        assert T2 in c.next_action

    def test_queue_exhausted_names_the_stop(self, tmp_git_repo: Path, monkeypatch) -> None:
        """Last item done: run_next will stop the run with a report — the
        card says so, or the owner closes the run now."""
        proj = _project(tmp_git_repo)
        _make_live(proj)
        self._done(proj, monkeypatch, [T])

        b = api.brief(proj)
        assert "awf_run_next" in b.next_action
        assert "awf_run_finish" in b.next_action


class TestBrokenStateRecovery:
    """Scenario 4: broken/partial run state (truncated file, garbage in
    decisions, missing plan fields, corrupt budget) degrades with a
    warning, without a traceback — on the read surfaces AND on the
    run_next gate (class NEG-3)."""

    def test_truncated_run_yaml_degrades_all_surfaces(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, None, raw="active: true\nqueue: [TODO-")  # cut mid-file
        before = run_state.run_file(proj).read_text(encoding="utf-8")

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        st = api.run_status(proj)
        assert b.run_warning and "run.yaml" in b.run_warning
        assert c.run_warning == b.run_warning
        assert st.active is False
        assert st.goal == ""

        result = api.run_next(proj)  # the gate degrades to "no run", no crash
        assert result.action == "refused"
        assert "No active run" in result.message
        # the file is kept for inspection, byte for byte
        assert run_state.run_file(proj).read_text(encoding="utf-8") == before

    def test_invalid_shape_degrades_to_no_run(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": ["TODO-00"]})  # bad id
        st = api.run_status(proj)
        b = api.brief(proj)
        assert st.active is False
        assert b.run_warning
        assert b.run is None

    def test_garbage_decisions_degrade_entrywise(self, tmp_git_repo: Path) -> None:
        """Garbage in `decisions` (a non-dict entry, a dict without kind):
        the valid entry survives, the goal is not lost, no traceback."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 0,
                "current": T,
                "goal": "Stabilize the run state",
                "decisions": [
                    "garbage",
                    7,
                    {"kind": "reject", "todo_id": T, "reason": "real"},
                ],
            },
        )
        st = api.run_status(proj)
        assert st.goal == "Stabilize the run state"
        assert st.decisions == [
            {"ts": "", "kind": "reject", "todo_id": T, "reason": "real"}
        ]
        b = api.brief(proj)
        assert b.run_goal == "Stabilize the run state"
        assert b.run_last_decision == f"reject {T} — real"
        c = api.load_supervisor_context(proj)
        assert c.run_decisions == st.decisions

    def test_legacy_state_without_plan_fields(self, tmp_git_repo: Path) -> None:
        """A pre-M1.1 state (no goal/criteria/decisions): the surfaces read
        empty plan fields (absent is not corruption) and run_next degrades
        to its normal gates, not a crash."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [T],
                "index": 0,
                "started_at": run_state.now_iso(),
            },
        )
        st = api.run_status(proj)
        assert (st.goal, st.criteria, st.decisions) == ("", [], [])
        b = api.brief(proj)
        assert b.run_goal == ""
        assert b.run_warning == ""
        result = api.run_next(proj)
        assert result.action == "refused"  # the normal gate: TODO file missing
        assert "missing or empty" in result.message

    def test_corrupt_budget_stops_run_next_without_traceback(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        """A valid shape with a corrupt `budget_minutes` (hand-edited; the
        write path stores an int): run_next must STOP the run — the budget
        gate cannot be verified, and degrading to 0 would silently disable
        it (AUD02-09 safe direction) — and write the report, not raise.
        Red before the fix: a raw ValueError from `int("abc")` in the
        budget gate (and the same unguarded read in the report writer)."""
        proj = _project(tmp_git_repo)
        _write_todo(proj, T)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 0,
                "budget_minutes": "abc",
                "started_at": run_state.now_iso(),
            },
        )
        captured = _fake_start(monkeypatch, proj, boom=True)

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "corrupted" in result.message
        assert "budget_minutes" in result.message
        assert result.report_file
        assert Path(result.report_file).is_file()
        assert run_state.read_run(proj)["active"] is False
        assert captured["calls"] == 0  # nothing was launched
        # the report was written from the SAME corrupt state — it degrades too
        assert "Run report" in Path(result.report_file).read_text(encoding="utf-8")
