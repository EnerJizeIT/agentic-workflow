"""TODO-0306 (1): stale-stage detection — the frozen stage is never green.

Incident: the QA worker and the engine died, ``current.yaml`` froze for
1.5 h, and the dashboard still painted a live QA. The detector has two
sources: a dead engine pid, or an ``updated_at`` older than
``automation.stale_stage_minutes`` (default 15). Every surface (card,
status, dashboard) renders the same explicit ``STALE stage … — dead: …``
line.
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


def _write_pipeline(proj, stage="agent-qa-review"):
    """A live definition naming the stage: the real project has one and its
    kind is ``execute``, so ``expected_action`` says "worker stage running …
    Do NOT intervene" — the contradiction the advice must not keep next to
    the STALE line. Without the file the advice test passes for the wrong
    reason (no other branch claimed the answer)."""
    path = proj / ".agentic" / "pipelines" / "default.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "name: default\nstages:\n- name: plan\n  role: supervisor\n"
        f"- name: {stage}\n  role: {stage}\n"
        "- name: verify\n  role: supervisor\n",
        encoding="utf-8",
    )


def test_default_threshold_marks_quiet_stage_stale(tmp_path):
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=20)

    stale = detect_stale_stage(proj, state)
    assert stale is not None
    assert stale.line.startswith("STALE stage agent-qa-review")
    assert "dead" in stale.line
    assert "20 min" in stale.line and "15 min" in stale.line


def test_fresh_stage_is_not_stale(tmp_path):
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    assert detect_stale_stage(proj, _state(proj, minutes_ago=1)) is None


def test_dead_pid_is_stale_even_with_fresh_updated_at(tmp_path):
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    # 2**31 is outside the OS pid range — cannot be alive.
    state = _state(proj, minutes_ago=0, pid=2**31)

    stale = detect_stale_stage(proj, state)
    assert stale is not None
    assert f"pid {2**31} dead" in stale.line


def test_config_threshold_override(tmp_path):
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: stale\nautomation:\n  stale_stage_minutes: 60\n",
        encoding="utf-8",
    )
    state = _state(proj, minutes_ago=20)
    assert detect_stale_stage(proj, state) is None


def test_broken_threshold_degrades_to_default(tmp_path):
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: stale\nautomation:\n  stale_stage_minutes: nope\n",
        encoding="utf-8",
    )
    assert detect_stale_stage(proj, _state(proj, minutes_ago=20)) is not None


def test_status_and_brief_carry_the_stale_line(tmp_git_repo):
    from awf import api
    from awf.api import get_status
    from awf.brief import build_brief

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=20)

    status = get_status(proj)
    assert status.stale_stage
    assert status.stale_stage.startswith("STALE stage agent-qa-review")
    assert f"({T})" in status.stale_stage

    card = build_brief(proj, status=status)
    assert f"- {status.stale_stage}" in card.text
    assert "- engine_dead:" not in card.text


def test_dashboard_badge_is_stale_not_green(tmp_path):
    from awf.api.dashboard import _determine_status

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=20)

    status, _cls, text, _icon, _label = _determine_status(state, proj)
    assert status == "dead"
    assert text.startswith("STALE stage")


def test_live_heartbeat_keeps_a_long_stage_green(tmp_path):
    """TODO-0306 (architect review): ``updated_at`` moves on stage start and
    signals only — a productive 20-minute stage would otherwise read as
    dead. A fresh heartbeat of the same pid is the engine-alive proof
    ``detect_engine_dead`` already uses."""
    from awf._engine_liveness import detect_stale_stage, write_heartbeat
    from awf.api.dashboard import _determine_status

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=20)  # pid = this test process
    write_heartbeat(proj, stage="agent-qa-review", todo_id=T)

    assert detect_stale_stage(proj, state) is None
    status, _cls, _text, _icon, _label = _determine_status(state, proj)
    assert status == "running"


def test_foreign_heartbeat_is_not_trusted(tmp_path):
    """The heartbeat of ANOTHER engine says nothing about this pid: the age
    rule stays in force instead of the guard silencing the detector."""
    import json

    from awf._engine_liveness import detect_stale_stage, write_heartbeat

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=20)
    write_heartbeat(proj, stage="agent-qa-review", todo_id=T)
    beat = proj / ".agentic" / "state" / "engine.heartbeat"
    payload = json.loads(beat.read_text(encoding="utf-8"))
    payload["pid"] = 2**31
    beat.write_text(json.dumps(payload), encoding="utf-8")

    stale = detect_stale_stage(proj, state)
    assert stale is not None and "no state event for 20 min" in stale.line


# ── QA (TODO-0306): the guards around the detector ──────────────────────


def test_dashboard_salvage_is_not_painted_stale(tmp_path):
    """A deliberate stop owns its badge: salvage is not a freeze, so a quiet
    ``updated_at`` must not turn it into ``STALE … dead``."""
    from awf.api.dashboard import _determine_status

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=20)
    state["salvage_needed"] = True
    state["salvage_stage"] = "agent-qa-review"

    status, _cls, text, _icon, _label = _determine_status(state, proj)
    assert status == "salvage", text
    assert "STALE" not in text


def test_done_leftover_is_not_stale(tmp_path):
    """``{phase: done}`` is post-completion residue, not a frozen stage."""
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    state = _state(proj, minutes_ago=120)
    state["phase"] = "done"

    assert detect_stale_stage(proj, state) is None


def test_status_keeps_a_live_pipeline_green(tmp_git_repo, monkeypatch):
    """The launch window (a live engine next to a leftover state) must not
    read as STALE: a FRESH heartbeat of the same pid is the detector's own
    proof of life, so the shared stale rule stays quiet here."""
    from awf import api
    from awf._engine_liveness import write_heartbeat
    from awf.api import get_status

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=20)
    write_heartbeat(proj, stage="agent-qa-review", todo_id=T)

    monkeypatch.setattr(
        "awf.api.lifecycle.check_pipeline_running",
        lambda _p, **_kw: (True, os.getpid(), None),
    )
    status = get_status(proj)

    assert status.pipeline_running is True
    assert status.stale_stage is None


def test_live_pid_with_stale_heartbeat_is_stale_on_both_surfaces(
    tmp_git_repo, monkeypatch
):
    """PA-0306-3: a live pid proves nothing when the heartbeat is 600 s old
    and ``updated_at`` is 20 min old. ``get_status`` and the dashboard must
    give ONE verdict (the STALE line), and the advice must not be "go idle"
    next to a frozen stage — that is the silent wait of incident 1."""
    from awf import api
    from awf._engine_liveness import write_heartbeat
    from awf.api import get_status
    from awf.api.dashboard import generate_state_dict
    from awf.brief import next_action_from_status

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _write_pipeline(proj)
    _state(proj, minutes_ago=20)
    write_heartbeat(
        proj,
        stage="agent-qa-review",
        todo_id=T,
        now=datetime.now(timezone.utc) - timedelta(seconds=600),
    )
    monkeypatch.setattr(
        "awf.api.lifecycle.check_pipeline_running",
        lambda _p, **_kw: (True, os.getpid(), None),
    )

    status = get_status(proj)
    assert status.pipeline_running is True
    assert status.stale_stage is not None
    assert status.stale_stage.startswith("STALE stage agent-qa-review")
    assert "no state event for 20 min" in status.stale_stage

    payload = generate_state_dict(proj)
    assert payload["status"] == "dead"
    assert payload["status_text"] == status.stale_stage, "one verdict, two surfaces"

    # The structural advice on a real project (a pipeline naming the
    # stage) is "worker stage running … Do NOT intervene" — the exact
    # silent wait of incident 1. The stale advice must outrank it.
    assert status.expected_action and "Do NOT intervene" in status.expected_action
    advice = next_action_from_status(status)
    assert "Do NOT intervene" not in advice, advice
    assert "go idle" not in advice
    assert "stale" in advice.lower()


def test_cli_status_prints_stale_without_a_running_pipeline(tmp_git_repo, capsys):
    """PA-0306-2: ``awf status`` stayed silent about a dead stage unless the
    resolver called the pipeline "running". A dead pid plus a 90-minute-old
    ``updated_at`` must reach stdout as STALE/dead — the CLI is the owner's
    surface, not a code caller reading the dataclass."""
    from awf import api, cli

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=90, pid=2**31)

    rc = cli.main(["status", "--project-dir", str(proj)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "STALE" in out and "dead" in out, out
    assert "pid 2147483648 dead" in out, out


def test_detector_owns_the_deliberate_stop_guard(tmp_path):
    """TODO-0306 (architect review): salvage and checkpoint are deliberate
    stops, not freezes — the guard lives in the detector so the card,
    status and dashboard inherit ONE rule instead of a caller-local copy."""
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    for flag in ("salvage_needed", "checkpoint_pending"):
        state = _state(proj, minutes_ago=20)
        state[flag] = True
        assert detect_stale_stage(proj, state) is None, flag


# ── QA (TODO-0306): граница, деградация порога и поверхность /api/state ─


def test_threshold_boundary_is_inclusive(tmp_path):
    """Порог — «старше или равно»: ровно 15 минут уже STALE, 14:59 — ещё нет.
    Офф-бай в сравнении (`>` вместо `>=`) спрятал бы ровно тот момент, когда
    владелец впервые смотрит на застывшую стадию."""
    from awf._engine_liveness import detect_stale_stage

    proj = _project(tmp_path)
    base = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    state = {
        "stage_name": "agent-qa-review",
        "stage_kind": "execute",
        "todo_id": T,
        "updated_at": base.isoformat(),
    }

    at_limit = (base + timedelta(minutes=15)).timestamp()
    stale = detect_stale_stage(proj, state, now=at_limit)
    assert stale is not None and "15 min" in stale.line

    under = (base + timedelta(minutes=14, seconds=59)).timestamp()
    assert detect_stale_stage(proj, state, now=under) is None


def test_non_positive_threshold_keeps_the_detector_on(tmp_path):
    """``0``/отрицательный порог — опечатка, а не выключатель: детектор по
    умолчанию ВКЛ (инцидент был молчаливым застыванием). Молча выключить
    его значением из конфига значило бы вернуть ту же зелёную картинку."""
    from awf._engine_liveness import detect_stale_stage

    for raw in ("0", "-5"):
        proj = _project(tmp_path / raw.replace("-", "neg"))
        (proj / ".agentic" / "config.yaml").write_text(
            f"project:\n  name: stale\nautomation:\n  stale_stage_minutes: {raw}\n",
            encoding="utf-8",
        )
        state = _state(proj, minutes_ago=20)
        assert detect_stale_stage(proj, state) is not None, raw


def test_dashboard_state_dict_never_paints_a_frozen_stage_running(tmp_git_repo):
    """Инцидент смотрел на дашборд: /api/state — та поверхность, которую
    владелец видит в браузере. Мёртвый pid + застывший ``updated_at`` не
    должны давать ``status: running`` (зелёный QA)."""
    from awf import api
    from awf.api.dashboard import generate_state_dict

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=90, pid=2**31)

    payload = generate_state_dict(proj)

    assert payload["status"] == "dead"
    assert payload["status_text"].startswith("STALE stage"), payload["status_text"]


def test_card_dead_pid_shows_the_one_stale_line_and_a_resume_hint(tmp_git_repo):
    """Мёртвый pid — самый частый вид инцидента. Карточка показывает ОДНУ
    строку STALE (не второй ``engine_dead`` рядом), а совет ведёт к
    ``awf_continue`` — иначе владелец видит факт без следующего шага."""
    from awf import api
    from awf.api import get_status
    from awf.brief import build_brief, next_action_from_status

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj, minutes_ago=0, pid=2**31)

    status = get_status(proj)
    assert status.engine_dead, "мёртвый pid обязан дать engine_dead"
    assert status.stale_stage and status.stale_stage.startswith("STALE stage")

    card = build_brief(proj, status=status)
    assert f"- {status.stale_stage}" in card.text
    assert "- engine_dead:" not in card.text, "один факт — одна строка"
    assert "awf_continue" in next_action_from_status(status)
