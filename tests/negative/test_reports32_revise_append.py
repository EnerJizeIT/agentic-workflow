"""REPORTS32 (TODO-0198): awf_run_revise — добавление и перестановка
NOT-STARTED юнитов в активный ран.

До фикса вклинить новый юнит в активный ран нельзя:
``_revision_items`` давал ровно два исхода (change / conflict),
``entry is None`` → конфликт "not in the run queue" — доборные юниты
приходилось гонять standalone ПОСЛЕ закрытия рана (репорт
awf-bug-20261011-recovery-i-commit-gate, п.5).

Третий исход — APPEND/INSERT:
1. Append: todo_id не из очереди → в КОНЕЦ очереди, если TODO-файл
   есть в inbox, id не completed/current и не дубль. Класс "appended"
   в changes / в записи ревизии / в тексте ответа.
2. Insert: опциональный ``position`` — позиция нового элемента в
   очереди; валидная зона — NOT-STARTED (``index``..``len(queue)``,
   ``len`` = append); невалидная позиция → отказ. Класс "inserted".
3. CAS/generation, stop_running, idempotency-key — как в v1 (не
   ослаблены); пустой pipeline у добавленного = config default.
4. Preview показывает appended/inserted без side effects.

Pipeline spawn подменяется (hermetic), как в test_run_revise.py.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import awf.api.pipeline as api_pipeline
from awf import api, run_state, todos

T1, T2, T3 = "TODO-0001", "TODO-0002", "TODO-0003"
NEW, NEW2 = "TODO-0004", "TODO-0005"
ALT = "alt"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="ReviseAppend")
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
    """run_start + one launch: index=1, current=T1, the rest not started."""
    api.run_start(proj, queue=queue)
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"


def _write_pipeline(proj: Path, name: str) -> None:
    pipelines = proj / ".agentic" / "pipelines"
    pipelines.mkdir(parents=True, exist_ok=True)
    (pipelines / f"{name}.yaml").write_text(
        f'name: "{name}"\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n',
        encoding="utf-8",
    )


def _raw_write(proj: Path, state: dict) -> None:
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")


def _queue_ids(state: dict) -> list[str]:
    return [str(q.get("todo_id", "")) for q in state.get("queue") or []]


def _pipelines(state: dict) -> dict[str, str]:
    return {
        str(q.get("todo_id", "")): str(q.get("pipeline", "") or "")
        for q in state.get("queue") or []
        if isinstance(q, dict)
    }


# ─── (a) append ──────────────────────────────────────────────────────────


def test_append_grows_the_queue_with_class(tmp_git_repo: Path, monkeypatch) -> None:
    """(a) append нового TODO в активный ран: очередь растёт (элемент в
    КОНЦЕ, порядок прежних сохранён), класс "appended" в changes, в записи
    ревизии и в тексте ответа; позиция рана не тронута. Red до фикса:
    результат "refused" с конфликтом "not in the run queue"."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": NEW, "pipeline": ALT}], reason="jql dobor", key="k1"
    )
    assert result.action == "applied"
    appended = [c for c in result.changes if c.get("class") == "appended"]
    assert [c["todo_id"] for c in appended] == [NEW]
    assert appended[0]["to"] == ALT

    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3, NEW], "the new element stands at the END"
    assert _pipelines(state) == {T1: "", T2: "", T3: "", NEW: ALT}
    assert state["index"] == 1 and state["current"] == T1

    entry = state["revisions"][0]
    assert entry["changes"] == [
        {"todo_id": NEW, "from": "", "to": ALT, "class": "appended"}
    ]
    assert "appended" in (result.message + " " + result.next_action)


def test_append_with_empty_pipeline_is_config_default(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Пустой pipeline у добавленного = config default (как у обычного
    элемента): в очереди лежит ""."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, NEW):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2])

    result = api.run_revise(proj, queue=[{"todo_id": NEW}], reason="why", key="k1")
    assert result.action == "applied"
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, NEW]
    assert _pipelines(state)[NEW] == ""


def test_append_then_run_next_launches_the_new_element(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Добавленный юнит не декорация: штатный run_next добирается до него
    в порядке очереди и запускает с закреплённым pipeline."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW):
        _write_todo(proj, t)
    _write_pipeline(proj, ALT)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    api.run_revise(
        proj, queue=[{"todo_id": NEW, "pipeline": ALT}], reason="why", key="k1"
    )

    for done_id in (T1, T2, T3):
        todos.archive_todo(proj, done_id)
        nxt = api.run_next(proj)
        assert nxt.action == "started", nxt.message

    state = run_state.read_run(proj)
    # index advanced at launch: the 4th element runs, index = 4
    assert state["current"] == NEW and state["index"] == 4
    assert _pipelines(state)[NEW] == ALT


# ─── (b) insert by position ──────────────────────────────────────────────


def test_insert_at_position_before_first_queued(tmp_git_repo: Path, monkeypatch) -> None:
    """(b) insert позицией: position=index → ПЕРЕД первым queued-элементом;
    класс "inserted" в changes и в записи ревизии."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": NEW, "pipeline": ALT}],
        reason="why", key="k1", position=1,
    )
    assert result.action == "applied"
    inserted = [c for c in result.changes if c.get("class") == "inserted"]
    assert [c["todo_id"] for c in inserted] == [NEW]

    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, NEW, T2, T3]
    assert state["index"] == 1 and state["current"] == T1
    assert state["revisions"][0]["changes"] == [
        {"todo_id": NEW, "from": "", "to": ALT, "class": "inserted"}
    ]


def test_insert_mid_queued_and_multiple_keep_request_order(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """position > index — внутри queued-зоны; несколько новых элементов
    встают в порядке запроса."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW, NEW2):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": NEW, "pipeline": ALT}, {"todo_id": NEW2, "pipeline": "alt2"}],
        reason="why", key="k1", position=2,
    )
    assert result.action == "applied"
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, NEW, NEW2, T3]
    assert _pipelines(state) == {T1: "", T2: "", NEW: ALT, NEW2: "alt2", T3: ""}


def test_mixed_request_pipeline_change_and_append(tmp_git_repo: Path, monkeypatch) -> None:
    """Один вызов: смена pipeline у стоящего элемента И append нового —
    оба применяются, класс только у добавленного."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj,
        queue=[{"todo_id": T2, "pipeline": ALT}, {"todo_id": NEW, "pipeline": "alt2"}],
        reason="why", key="k1",
    )
    assert result.action == "applied"
    by_id = {c["todo_id"]: c for c in result.changes}
    assert by_id[T2].get("class") is None
    assert by_id[NEW]["class"] == "appended"
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3, NEW]
    assert _pipelines(state) == {T1: "", T2: ALT, T3: "", NEW: "alt2"}


# ─── (c) refusals ────────────────────────────────────────────────────────


def test_append_refused_when_todo_file_missing(tmp_git_repo: Path, monkeypatch) -> None:
    """(c) отказ: TODO-файла нет в inbox — новый элемент не встаёт в
    очередь (иначе run_next не смог бы его запустить); очередь и ревизии
    не тронуты."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    # NEW deliberately NOT written to the inbox
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": NEW, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert NEW in result.conflicts[0]["todo_id"]
    assert "TODO file" in result.conflicts[0]["reason"]
    assert not result.conflicts[0]["reason"].startswith("not in the run queue")
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3]
    assert not state.get("revisions")


def test_append_refused_when_duplicate_in_queue(tmp_git_repo: Path, monkeypatch) -> None:
    """(c) дубль: id уже стоит в очереди (started) → существующий конфликт
    "already started", append не происходит."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _raw_write(proj, {
        "active": True,
        "queue": [{"todo_id": t, "pipeline": ""} for t in (T1, T2, T3)],
        "index": 2, "current": T2, "completed": [],
        "generation": 1, "started_at": run_state.now_iso(),
    })

    result = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}], reason="why", key="k1"
    )
    assert result.action == "refused"
    assert result.conflicts[0]["reason"] == "already started"
    assert _queue_ids(run_state.read_run(proj)) == [T1, T2, T3]


def test_append_refused_when_completed_or_current(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(c) completed / current: id из completed-списка или текущий id —
    даже не стоя в очереди, в append не встают (существующие причины)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3):
        _write_todo(proj, t)
    _raw_write(proj, {
        "active": True,
        "queue": [{"todo_id": t, "pipeline": ""} for t in (T1, T2, T3)],
        "index": 2, "current": T2, "completed": [T1],
        "generation": 1, "started_at": run_state.now_iso(),
    })

    res_completed = api.run_revise(
        proj, queue=[{"todo_id": T1, "pipeline": ALT}], reason="why", key="k1"
    )
    assert res_completed.action == "refused"
    assert res_completed.conflicts[0]["reason"] == "already completed"

    res_current = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="why", key="k2"
    )
    assert res_current.action == "refused"
    assert "current" in res_current.conflicts[0]["reason"]
    assert _queue_ids(run_state.read_run(proj)) == [T1, T2, T3]
    assert not run_state.read_run(proj).get("revisions")


def test_invalid_position_refuses(tmp_git_repo: Path, monkeypatch) -> None:
    """(c) невалидная позиция → отказ: < index (до started/current),
    > len(queue), не int, отрицательная. Ничего не пишется."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    for bad in (0, -1, 99, "one", 1.5):
        result = api.run_revise(
            proj,
            queue=[{"todo_id": NEW, "pipeline": ALT}],
            reason="why", key="k1", position=bad,
        )
        assert result.action == "refused", (bad, result.action)
        assert "position" in result.message
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3]
    assert not state.get("revisions")


# ─── (d) preview without side effects ────────────────────────────────────


def test_preview_shows_appended_and_inserted_without_side_effects(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(d) preview показывает appended/inserted (с классами) и не пишет
    ничего — ни очередь, ни ревизии."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW, NEW2):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    preview = api.run_revise(
        proj,
        queue=[{"todo_id": T2, "pipeline": ALT}, {"todo_id": NEW, "pipeline": "alt2"}],
        reason="why", key="k1", preview=True,
    )
    assert preview.action == "preview" and preview.preview is True
    by_id = {c["todo_id"]: c for c in preview.changes}
    assert by_id[T2].get("class") is None
    assert by_id[NEW]["class"] == "appended"
    assert preview.conflicts == []

    preview_ins = api.run_revise(
        proj,
        queue=[{"todo_id": NEW2, "pipeline": ALT}],
        reason="why", key="k1", position=1, preview=True,
    )
    assert preview_ins.action == "preview"
    assert preview_ins.changes[0]["class"] == "inserted"

    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3]
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}
    assert not state.get("revisions")


# ─── (e) idempotency by key survives the new outcomes ────────────────────


def test_repeat_with_same_key_is_noop_for_append(tmp_git_repo: Path, monkeypatch) -> None:
    """(e) тот же key + тот же запрос — no-op; другой append под тем же
    key — отказ ``different payload`` (TODO-0240 review v7, AA1)."""
    proj = _project(tmp_git_repo)
    for t in (T1, T2, T3, NEW, NEW2):
        _write_todo(proj, t)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    first = api.run_revise(proj, queue=[{"todo_id": NEW}], reason="why", key="k9")
    assert first.action == "applied"

    again = api.run_revise(
        proj, queue=[{"todo_id": NEW2, "pipeline": ALT}], reason="why", key="k9"
    )
    assert again.action == "refused"
    assert "different payload" in again.message
    state = run_state.read_run(proj)
    assert _queue_ids(state) == [T1, T2, T3, NEW], "the repeat must not append NEW2"
    assert len(state.get("revisions") or []) == 1
