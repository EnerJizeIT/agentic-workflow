"""ORCH M1.2: brief and load_supervisor_context — two views of one record.

The run (забег) state in `.agentic/state/run.yaml` is read by ONE shared
reader (`awf/run_plan_read.read_run_record`); `awf_brief` renders the
compact view (goal, position, budget, last decision in one line, links
to sources) and `awf_load_supervisor_context` renders the full view (the
same record plus all decisions and the vision/plan excerpts). Not
parallel retellings (IMPLEMENTATION-STRATEGY §1.2). A broken run.yaml
degrades both surfaces to "no run" with a warning — no traceback.

All tests go through the public API on a synthetic project (no mocks):
run_start / reject_commit write the state, brief / load_supervisor_context
read it back.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from awf import api, run_state

T = "TODO-0001"
T2 = "TODO-0002"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Ctx")
    return tmp_git_repo


def _raw_write(proj: Path, state: object, raw: str | None = None) -> None:
    """Write run.yaml exactly as given — simulates an on-disk corruption
    the sanitized reader must survive (the normal write path cannot
    produce it)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(raw if raw is not None else yaml.safe_dump(state, allow_unicode=True),
                 encoding="utf-8")


def test_brief_shows_run_goal_and_last_decision(tmp_git_repo: Path) -> None:
    """(a) the card shows the run goal, the last decision in one line, and
    links to the sources (run.yaml + the decision's file). Red on baseline:
    the card has no run plan fields at all."""
    proj = _project(tmp_git_repo)
    api.run_start(
        proj,
        queue=[T, T2],
        goal="Unify the supervisor context",
        criteria=["one reader", "card + full view"],
    )
    api.reject_commit(proj, T, "diff too wide")

    b = api.brief(proj)
    assert b.run_goal == "Unify the supervisor context"
    assert b.run_last_decision == f"reject {T} — diff too wide"
    assert ".agentic/state/run.yaml" in b.run_sources
    assert f"outbox/REVIEW-{T}.md" in b.run_sources
    # and the rendered card carries them (not just the dataclass fields)
    assert "run goal: Unify the supervisor context" in b.text
    assert "run last decision: reject" in b.text
    assert "run sources:" in b.text
    assert "outbox/REVIEW-TODO-0001.md" in b.text


class TestContextFullView:
    def test_context_shows_run_goal_and_all_decisions(self, tmp_git_repo: Path) -> None:
        """(b) the full context shows the same record plus ALL decisions
        (which, why, when) and the same sources. Red on baseline: the
        context has no run fields at all."""
        proj = _project(tmp_git_repo)
        api.run_start(
            proj, queue=[T, T2],
            goal="Unify the supervisor context", criteria=["one reader"],
        )
        api.reject_commit(proj, T, "diff too wide")
        api.reject_commit(proj, T2, "tests red")

        c = api.load_supervisor_context(proj)
        assert c.run_active is True
        assert c.run_goal == "Unify the supervisor context"
        assert c.run_criteria == ["one reader"]
        assert c.run_position == "1/2"
        assert [(d["kind"], d["todo_id"], d["reason"]) for d in c.run_decisions] == [
            ("reject", T, "diff too wide"),
            ("reject", T2, "tests red"),
        ]
        assert all(d["ts"] for d in c.run_decisions)  # when
        assert c.run_last_decision == f"reject {T2} — tests red"
        assert ".agentic/state/run.yaml" in c.run_sources
        assert f"outbox/REVIEW-{T2}.md" in c.run_sources
        # two views of the SAME record: the card shows the same goal and
        # the same last decision line
        b = api.brief(proj)
        assert b.run_goal == c.run_goal
        assert b.run_last_decision == c.run_last_decision

    def test_card_clips_long_goal_context_keeps_full(self, tmp_git_repo: Path) -> None:
        """The card keeps its word budget: a long goal is clipped; the full
        context carries it intact. The record is one, the views differ."""
        long_goal = " ".join(f"w{i}" for i in range(60))
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T], goal=long_goal)
        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_goal != long_goal
        assert len(b.run_goal.split()) <= 11  # 10 words + the ellipsis
        assert c.run_goal == long_goal


class TestRejectAgreement:
    def test_after_reject_both_surfaces_agree(self, tmp_git_repo: Path) -> None:
        """(c) after a reject in a run: both surfaces show the decision
        (reject) and the same next permitted action (run-aware: the run
        owns the queue position, so awf_run_next — not the generic
        awf_start)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T], goal="Stabilize the run state")
        api.reject_commit(proj, T, "missing regression test")

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_last_decision == c.run_last_decision
        assert b.run_last_decision.startswith(f"reject {T}")
        assert "awf_run_next" in b.next_action
        assert "awf_run_next" in c.next_action
        assert "awf_start" not in b.next_action
        assert "awf_start" not in c.next_action

    def test_outside_run_behavior_unchanged(self, tmp_git_repo: Path) -> None:
        """No run: no run lines on the card, no run fields in the context,
        no warning, the next_action is the pre-M1.2 one (no run-aware
        override)."""
        proj = _project(tmp_git_repo)
        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_warning == ""
        assert b.run_goal == ""
        assert b.run_last_decision == ""
        assert b.run_sources == []
        assert "run goal" not in b.text
        assert "run last decision" not in b.text
        assert "run sources" not in b.text
        assert "run warning" not in b.text
        assert c.run_active is False
        assert c.run_goal == ""
        assert c.run_decisions == []
        assert c.run_sources == []
        assert "awf_run_next" not in c.next_action


class TestBrokenRunYaml:
    def test_broken_shape_degrades_with_warning(self, tmp_git_repo: Path) -> None:
        """(d) a run.yaml with an invalid shape (empty queue) degrades to
        "no run" on BOTH surfaces with a warning — no traceback, the file
        is kept for inspection."""
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": []})

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_warning
        assert "run.yaml" in b.run_warning
        assert b.run_warning in b.text
        assert c.run_warning == b.run_warning
        assert any("run.yaml" in w for w in c.warnings)
        assert b.run is None
        assert c.run_active is False
        assert c.run_goal == ""

    def test_broken_yaml_degrades_with_warning(self, tmp_git_repo: Path) -> None:
        """(d) broken YAML in run.yaml: the same degradation — both
        surfaces work, the warning is present, no traceback."""
        proj = _project(tmp_git_repo)
        _raw_write(proj, None, raw="active: [unclosed")

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert b.run_warning
        assert c.run_warning
        assert b.run is None
        assert c.run_active is False
        assert c.run_decisions == []

    def test_corrupt_budget_in_valid_shape_degrades(self, tmp_git_repo: Path) -> None:
        """(d, invariant 5) a run.yaml with a VALID shape but a corrupt
        ``budget_minutes`` (hand-edited; the write path cannot produce it):
        both surfaces must still work with a warning, no traceback (the
        shared reader promises 'never raises on file state'; the M1.1
        precedent degrades corrupt field values, it does not crash).
        Red before the fix: ValueError in read_run_record's int(budget)."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 0,
                "budget_minutes": "abc",
            },
        )

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert c.run_budget_minutes == 0
        assert b.run_warning or c.run_warning
