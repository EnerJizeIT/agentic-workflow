"""REPORTS33: завершённый юнит не «current», никто не в двух состояниях.

Репорт (awf-bug-20261012-awf-run-revise-pokazyvaet-zavershennyy-yunit-kak-
current.md, opencode-session-manager 1.8.0): после done-события 0011
(заархивирован, коммит) ответ ``awf_run_revise`` показал
``current_queue = [{0011, state: "current"}]``; позже ``awf_run_status``
при обоих закрытых юнитах показал 0012 ОДНОВРЕМЕННО в ``completed`` и в
``current`` (position 2/2).

Корень: завершённый ``current`` кредитуется в ``completed`` только на
СЛЕДУЮЩЕМ запуске (run_next, REPORTS26 B3). Пока это не случилось,
read-time-вид (``_completed_view``) уже несёт его в ``completed``, а поле
``current`` и разметка revise всё ещё указывают на него: один id в двух
состояниях.

Инварианты (TODO-0205):
1. state элемента в current_queue: completed-элементы помечены
   ``completed``; ``current`` — только фактически выполняемый (живой
   пайплайн) или следующий незапущенный; остальные — ``queued``.
   Завершённый юнит никогда не ``current``.
2. run_status после done не показывает один id и в completed, и в
   current; position согласован (после завершения последнего элемента:
   current пуст или следующий незапущенный).
3. Preview и apply используют одну и ту же честную разметку (один
   классификатор).
4. CAS/generation/конфликты не трогать.
"""
from __future__ import annotations

from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state, todos

T1, T2, T3 = "TODO-0001", "TODO-0002", "TODO-0003"
ALT = "alt"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports33")
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


def _archive(proj: Path, todo_id: str) -> None:
    """done-событие юнита: архив (inbox → done/{id}/, .ready потреблён)."""
    assert todos.archive_todo(proj, todo_id) is not None


def _states(result) -> dict[str, str]:
    return {e["todo_id"]: e["state"] for e in result.current_queue}


# ─── (a) revise: done-юнит — completed, не current ──────────────────────


def test_revise_marks_done_unit_completed_not_current(tmp_git_repo: Path, monkeypatch) -> None:
    """(a) После done-события единственного юнита (заархивирован)
    revise-превью помечает его ``completed``, не ``current``; изменение
    пайплайна к нему — конфликт «already completed». Красный на
    baseline: state ``current`` (сырой список completed ещё не содержит
    заархивированный current)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"
    _archive(proj, T1)

    preview = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}],
        reason="why", key="k1", preview=True,
    )
    assert preview.action == "preview"
    assert _states(preview) == {T1: "completed"}, (
        "a done unit is never 'current' in the queue listing"
    )

    apply_ = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}], reason="why", key="k1"
    )
    assert apply_.action == "refused"
    assert apply_.conflicts == [{"todo_id": T1, "reason": "already completed"}]
    state = run_state.read_run(proj)
    assert not state.get("revisions"), "the refused apply must record nothing"


# ─── (b) run_status: один id не в двух состояниях ────────────────────────


def test_run_status_closed_tail_no_id_in_two_states(tmp_git_repo: Path, monkeypatch) -> None:
    """(b) Оба юнита закрыты: run_status не показывает один id и в
    completed, и в current; position 2/2 согласован (хвост закрыт —
    current пуст). Красный на baseline: current=TODO-0002 и
    completed=[TODO-0001, TODO-0002] одновременно."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    api.run_start(proj, queue=[T1, T2])
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"
    _archive(proj, T1)
    assert api.run_next(proj).action == "started"
    _archive(proj, T2)

    status = api.run_status(proj)
    assert set(status.completed) == {T1, T2}
    assert status.current not in status.completed, (
        "an id must not sit in completed and current at once"
    )
    assert status.current == "", "queue exhausted — nothing is running"
    assert status.position == "2/2"

    # чтение не трогает run.yaml — index/current владение run_next (B3)
    state = run_state.read_run(proj)
    assert state["index"] == 2 and state["current"] == T2
    assert list(state["completed"]) == [T1]


def test_run_status_midrun_current_is_next_not_started(tmp_git_repo: Path, monkeypatch) -> None:
    """(b) После done-события T1 (очередь продолжается): T1 в
    completed и НЕ current; current — следующий незапущенный (T2);
    position считает его. Красный на baseline: current=TODO-0001 при
    completed=[TODO-0001]."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    api.run_start(proj, queue=[T1, T2, T3])
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"
    _archive(proj, T1)

    status = api.run_status(proj)
    assert T1 in status.completed
    assert status.current == T2, "the finished current is no longer current"
    assert status.position == "2/3"

    state = run_state.read_run(proj)
    assert state["index"] == 1 and state["current"] == T1, (
        "the state file keeps run_next's ownership"
    )


# ─── (c) живой пайплайн: его юнит current, остальные queued/completed ───


def test_live_pipeline_its_unit_current_rest_queued_completed(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(c) Живой пайплайн → его юнит ``current`` (run_status и
    revise-превью), остальные — ``queued``/``completed``; живой юнит не
    кредитуется в completed. Гвардия: фикс (a)/(b) не должен начать
    прятать фактически выполняемый юнит."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    api.run_start(proj, queue=[T1, T2, T3])
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"
    _archive(proj, T1)
    assert api.run_next(proj).action == "started"  # T2 in flight
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )

    status = api.run_status(proj)
    assert status.current == T2
    assert T2 not in status.completed
    assert T1 in status.completed
    assert status.position == "2/3"

    preview = api.run_revise(
        proj, queue=[{"todo_id": T3, "pipeline": ALT}],
        reason="why", key="k1", preview=True,
    )
    assert _states(preview) == {T1: "completed", T2: "current", T3: "queued"}
