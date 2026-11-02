"""TODO-0346 (IMP2): one advice owner and the status fields the card needs.

The card (``awf_brief``) and ``awf_status`` must give the SAME next step for
every state: the state order lives in ``next_action_from_status``, the
run-aware layer (reject → ``awf_run_revise``/``awf_run_next``, approve →
``awf_run_next``) lives in ``advice_for`` on top of it. ``get_status`` fills
``next_action`` through that owner; ``build_brief`` uses the same call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api._results import StatusResult
from awf.brief import advice_for, build_brief
from awf.run_plan_read import RunRecord

T = "TODO-0001"
T2 = "TODO-0002"


def _status(**overrides) -> StatusResult:
    fields = {
        "project_name": "proj",
        "active_todos": [],
        "done_count": 0,
        "blocked_count": 0,
        "blocked_ids": [],
        "conflict_warning": None,
        "suggestion": None,
    }
    fields.update(overrides)
    return StatusResult(**fields)


def _reject_record() -> RunRecord:
    return RunRecord(
        active=True,
        goal="Ship it",
        state={
            "active": True,
            "queue": [{"todo_id": T, "pipeline": ""}],
            "index": 0,
            "decisions": [{"kind": "reject", "todo_id": T}],
        },
        last_decision={"kind": "reject", "todo_id": T, "reason": "red"},
    )


def _approve_record() -> RunRecord:
    return RunRecord(
        active=True,
        goal="Ship it",
        state={
            "active": True,
            "current": T,
            "queue": [{"todo_id": T, "pipeline": ""}, {"todo_id": T2, "pipeline": ""}],
            "index": 1,
            "decisions": [{"kind": "approve", "todo_id": T}],
        },
        last_decision={"kind": "approve", "todo_id": T},
    )


def _live_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic" / "state").mkdir(parents=True)
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(parents=True)
    (pipes / "default.yaml").write_text(
        "name: default\nstages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: worker\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    done = project / ".agentic" / "done" / "TODO-0000"
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# TODO-0000\n", encoding="utf-8")
    return project


# ─── the agreement table (S1–S13 + S1a) ─────────────────────────────────


ADVICE_TABLE = [
    (
        "S1 blocked",
        _status(blocked_ids=[T2]),
        None,
        "outbox/BLOCKED-",
    ),
    (
        "S2 budget",
        _status(budget_exhausted_todo=T2),
        None,
        T2,
    ),
    (
        "S3 engine_dead",
        _status(engine_dead=f"stage=worker, todo={T2}, pid dead", engine_dead_todo=T2),
        None,
        "awf_continue",
    ),
    (
        "S4 stale stage",
        _status(stale_stage="STALE stage worker", pipeline_running=True,
                expected_action="worker stage running — do NOT intervene"),
        None,
        "Stale stage",
    ),
    (
        "S5 zombie",
        _status(zombie_state="Zombie stage state — read State, then awf_continue"),
        None,
        "Zombie",
    ),
    (
        "S7 running",
        _status(pipeline_running=True,
                expected_action="worker stage running — auto-transition. Do NOT intervene."),
        None,
        "worker stage running",
    ),
    (
        "S8 verify waits",
        _status(pipeline_running=True, current_stage_name="verify",
                expected_action="VERIFY STAGE — pipeline waiting for YOUR decision."),
        None,
        "VERIFY STAGE",
    ),
    (
        "S9 active unit",
        _status(active_todos=[{"todo_id": T2}]),
        None,
        "TODO-0002",
    ),
    (
        "S10 suggestion",
        _status(suggestion="Do the thing."),
        None,
        "Do the thing.",
    ),
    (
        "S11a reject in run",
        _status(active_todos=[{"todo_id": T}]),
        _reject_record(),
        "awf_run_revise",
    ),
    (
        "S11b reject outside run",
        _status(active_todos=[{"todo_id": T}]),
        None,
        "TODO-0001",
    ),
    (
        "S12 approve",
        _status(active_todos=[{"todo_id": T2}]),
        _approve_record(),
        "awf_run_next",
    ),
    (
        "S13 checkpoint",
        _status(pipeline_running=True, checkpoint_pending=True,
                expected_action="BD-36 checkpoint form is open — WAIT for user confirmation."),
        None,
        "WAIT for user confirmation",
    ),
]


@pytest.mark.parametrize("name,status,record,needle", ADVICE_TABLE, ids=[r[0] for r in ADVICE_TABLE])
def test_status_and_brief_advice_agree(
    name: str, status: StatusResult, record: RunRecord | None, needle: str, tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    advice = advice_for(status, record)
    card = build_brief(project, status=status, run_record=record)

    assert advice
    assert needle in advice, f"{name}: expected {needle!r} in {advice!r}"
    assert card.next_action == advice, f"{name}: card and status disagree"
    # the card line keeps its word budget; the shared sentence starts it
    assert " ".join(advice.split()[:10]) in card.text


# ─── the run-aware layer does not bury an actionable fact ───────────────


def test_salvage_outranks_the_run_step(tmp_path: Path) -> None:
    project = _live_project(tmp_path)
    status = _status(
        salvage_needed=True,
        salvage_stage="worker",
        expected_action="Salvage (attempt 1): worker didn't signal.",
    )
    advice = advice_for(status, _reject_record())

    assert "Salvage" in advice
    assert "awf_run_revise" not in advice
    assert build_brief(project, status=status, run_record=_reject_record()).next_action == advice


def test_foreign_commit_outranks_salvage(tmp_path: Path) -> None:
    """AC46/INV10: the combination resolves to S1a, not S1.

    ``next_action_from_status`` ranks ``expected_action`` above
    ``foreign_commit``; the salvage prefill in ``get_status`` used to fill
    that field, so the owner heard "Salvage" and never the recovery step.
    Both surfaces must name the foreign commit.
    """
    project = _live_project(tmp_path)
    (project / ".agentic" / "state" / "current.yaml").write_text(
        "todo_id: TODO-0003\n"
        "salvage_needed: true\n"
        "salvage_stage: worker\n"
        "foreign_commit: 'FOREIGN COMMIT: HEAD moved during stage worker'\n"
        "updated_at: 2026-11-02T10:00:00+00:00\n",
        encoding="utf-8",
    )

    st = api.get_status(project)
    card = api.brief(project)

    assert "FOREIGN COMMIT" in st.next_action
    assert "Salvage" not in st.next_action
    assert card.next_action == st.next_action


# ─── the status fields reach the wire form ──────────────────────────────


def test_status_fields_reach_as_dict(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".agentic" / "state").mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: proj\n", encoding="utf-8"
    )
    (project / ".agentic" / "state" / "current.yaml").write_text(
        "todo_id: TODO-0007\nstage_name: worker\nupdated_at: 2026-11-02T10:00:00+00:00\n",
        encoding="utf-8",
    )

    st = api.get_status(project)
    payload = st.as_dict()

    assert st.current_todo == "TODO-0007"
    assert st.state_updated_at == "2026-11-02T10:00:00+00:00"
    assert payload["current_todo"] == "TODO-0007"
    assert payload["state_updated_at"] == "2026-11-02T10:00:00+00:00"
    assert payload["next_action"] == st.next_action
    assert isinstance(payload["foreign_commit"], (str, type(None)))
    assert isinstance(payload["replan_error"], (str, type(None)))


def test_get_status_and_brief_agree_after_reject(tmp_git_repo: Path) -> None:
    proj = tmp_git_repo
    api.init_project(proj, project_name="Ctx")
    api.run_start(proj, queue=[T], goal="One advice")
    api.reject_commit(proj, T, "tests red")

    st = api.get_status(proj)
    card = api.brief(proj)

    assert st.next_action
    assert "awf_run_revise" in st.next_action
    assert "awf_run_next" in st.next_action
    assert "awf_start" not in st.next_action
    assert card.next_action == st.next_action
