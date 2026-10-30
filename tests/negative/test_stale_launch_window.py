"""TODO-0306 (QA): границы stale-детектора — окно запуска и state без
маркеров стадии.

Отдельный файл, чтобы не перевалить ratchet-планку 400 строк у
``test_stale_stage_detect.py`` (он был на 396).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import yaml

T = "TODO-0306"


def _project(tmp_path):
    proj = tmp_path / "proj"
    for name in ("state", "context", "logs", "inbox", "outbox"):
        (proj / ".agentic" / name).mkdir(parents=True, exist_ok=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: stale\n", encoding="utf-8"
    )
    return proj


def _state(proj, *, minutes_ago=0, pid=None, stage="agent-qa-review"):
    stamp = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).replace(microsecond=0)
    data = {
        "stage_idx": 1,
        "stage_name": stage,
        "stage_kind": "execute",
        "todo_id": T,
        "pipeline_pid": pid if pid is not None else os.getpid(),
        "updated_at": stamp.isoformat(),
    }
    (proj / ".agentic" / "state" / "current.yaml").write_text(
        yaml.safe_dump(data, sort_keys=True), encoding="utf-8"
    )
    return data


def test_launch_window_foreign_live_pid_is_not_stale(tmp_path):
    """Окно запуска: резолвер уже видит ЖИВОЙ движок, а ``current.yaml`` ещё
    держит pid прошлого прогона. Другой pid — это остаток, а не застывшая
    стадия; иначе новый запуск красится в STALE на первой же секунде.
    Ветка ``state_pid != live_pid`` до сих пор не была покрыта напрямую:
    соседний тест проверяет СОВПАДАЮЩИЙ pid со свежим heartbeat."""
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=90, pid=2**31)  # pid прошлого прогона

    stale = detect_stale_stage(proj, state, live_pid=os.getpid())

    assert stale is None


def test_status_launch_window_with_foreign_live_pid_is_green(
    tmp_git_repo, monkeypatch
):
    """Та же граница на поверхности ``get_status``: резолвер вернул ЖИВОЙ pid
    нового движка, state — мёртвый pid старого. ``live_pid`` доезжает до
    детектора, поэтому строка STALE не появляется (живой запуск не наказан
    за остаток предыдущего)."""
    from awf import api
    from awf.api import get_status

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=90, pid=2**31)

    monkeypatch.setattr(
        "awf.api.lifecycle.check_pipeline_running",
        lambda _p, **_kw: (True, os.getpid(), "pid_file"),
    )
    status = get_status(proj)

    assert status.pipeline_running is True
    assert status.stale_stage is None


def test_state_without_stage_markers_is_left_to_df6_fallback(tmp_path):
    """State без имени стадии/TODO/вида стадии — не «застывшая стадия»:
    строке STALE нечего называть. Такой остаток остаётся за прежним
    DF6-6-путём дашборда (``Pipeline process dead``), а не превращается в
    ``STALE stage ?``."""
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    state = {
        "pipeline_pid": 2**31,
        "updated_at": (
            datetime.now(timezone.utc) - timedelta(minutes=90)
        ).isoformat(),
    }

    assert detect_stale_stage(proj, state) is None
