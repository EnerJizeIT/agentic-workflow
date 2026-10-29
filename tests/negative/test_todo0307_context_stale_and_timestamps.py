"""TODO-0307 (3, 4): the bootstrap carries STALE, one ``updated_at`` parser.

PA-0306-1: ``load_supervisor_context`` described engine death but not the
frozen stage, so the one-shot bootstrap could read as a calm "pipeline
running". The fact must travel AND the advice must come from
``next_action_from_status`` (the one owner of the advice order).

PA-0306-6: three readers parsed ``current.yaml``'s ``updated_at`` on their
own. One parser now serves them; the format/timezone cases are pinned here.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import yaml

T = "TODO-0307"


def _state(proj, *, minutes_ago=20, stage="agent-qa-review"):
    stamp = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).replace(microsecond=0)
    data = {
        "stage_idx": 1,
        "stage_name": stage,
        "stage_kind": "execute",
        "todo_id": T,
        "pipeline_pid": os.getpid(),
        "updated_at": stamp.isoformat(),
    }
    state_dir = proj / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "current.yaml").write_text(
        yaml.safe_dump(data, sort_keys=True), encoding="utf-8"
    )
    return data


def test_bootstrap_carries_stale_stage_and_the_owned_advice(tmp_git_repo):
    """A frozen stage reaches ``awf_load_supervisor_context`` as a field
    and as the next step — never the calm "pipeline running" line."""
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="stale")
    _state(proj)

    ctx = api.load_supervisor_context(proj)

    assert ctx.stale_stage.startswith("STALE stage agent-qa-review")
    assert f"({T})" in ctx.stale_stage
    assert ctx.stale_stage in ctx.as_dict()["stale_stage"]
    assert "stale" in ctx.next_action.lower(), ctx.next_action
    assert "go idle" not in ctx.next_action.lower()


def test_bootstrap_without_a_frozen_stage_keeps_the_old_answer(tmp_git_repo):
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="fresh")
    _state(proj, minutes_ago=1)

    ctx = api.load_supervisor_context(proj)

    assert ctx.stale_stage == ""
    assert "stale" not in ctx.next_action.lower()


def test_one_updated_at_parser_handles_formats_and_timezone():
    """``Z``, ``+00:00`` and a naive stamp mean the same instant; every
    reader delegates to the one parser instead of its own copy."""
    from awf import pipeline_state
    from awf._engine_liveness import _updated_at_age_minutes

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc).timestamp()
    expected_ts = datetime(
        2026, 10, 29, 11, 40, tzinfo=timezone.utc
    ).timestamp()
    for raw in (
        "2026-10-29T11:40:00+00:00",
        "2026-10-29T11:40:00Z",
        "2026-10-29T11:40:00",
    ):
        parsed = pipeline_state.parse_updated_at(raw)
        assert parsed is not None, raw
        assert parsed.tzinfo is not None, raw
        assert parsed.timestamp() == expected_ts, raw
        assert 19.9 <= _updated_at_age_minutes({"updated_at": raw}, now=now) <= 20.1

    for bad in (None, "", "not-a-date", 42):
        assert pipeline_state.parse_updated_at(bad) is None, bad
        assert _updated_at_age_minutes({"updated_at": bad}, now=now) is None
    # The third reader owns no parser of its own (architect review, TODO-0307):
    # a pass-through wrapper next to the shared one is the same drift, one
    # indirection later.
    import awf.stall_detect as stall_detect

    assert not hasattr(stall_detect, "_parse_updated_at")


def test_is_state_stale_keeps_its_fail_closed_policy():
    """The shared parser does not change the 2h reader: an unknown stamp
    still reads stale (its own policy), the freshness window is unchanged."""
    from awf import pipeline_state

    fresh = (
        datetime.now(timezone.utc) - timedelta(minutes=20)
    ).replace(microsecond=0).isoformat()
    assert not pipeline_state.is_state_stale({"updated_at": fresh})
    assert pipeline_state.is_state_stale({"updated_at": fresh}, max_age_seconds=60)
    assert pipeline_state.is_state_stale({"updated_at": "garbage"}) is True
    assert pipeline_state.is_state_stale({}) is True


def _run_with_a_frozen_stage(proj, *, minutes_ago=95):
    """An ACTIVE run whose stage state carries a dead pid and old stamps.

    A dead pid means engine_dead AND the STALE line (one fact, two readers),
    while the stock timestamps still trip the opt-in stall detector — the
    exact overlap TODO-0307 rules on: a quiet LIVE stage is a stall, a
    frozen stage is STALE, and a dead engine is never "quiet".
    """
    from awf import api

    api.run_start(proj, queue=[T], goal="gates green", criteria=["verify"])
    cfg_file = proj / ".agentic" / "config.yaml"
    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    cfg.setdefault("run", {})["stall_minutes"] = 30
    cfg_file.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    old = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).replace(microsecond=0)
    state_dir = proj / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "current.yaml").write_text(
        yaml.safe_dump(
            {
                "stage_name": "agent-qa-review",
                "stage_kind": "execute",
                "stage_idx": 1,
                "todo_id": T,
                "pipeline_pid": 2**31,
                "updated_at": old.isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    log_dir = proj / ".agentic" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    (log_dir / "orchestrator.log").write_text(
        f"[{stamp}] Pipeline started (default)\n"
        f"[{stamp}] Stage 1: agent-qa-review (worker :: execute)\n",
        encoding="utf-8",
    )


def test_run_status_drops_the_stall_line_next_to_engine_dead(tmp_git_repo):
    """Architect review, TODO-0307: a dead engine is not a "quiet" stage —
    the run message must not paint the stall line beside "Engine dead".
    The ``stall`` FIELD keeps the detector's fact; only the sentence drops
    it (the same split the brief card uses, PA-0306-5)."""
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="run-stale")
    _run_with_a_frozen_stage(proj)

    st = api.run_status(proj)

    assert st.stall, "the detector's fact stays in the field"
    assert st.engine_dead, "a dead pid is engine_dead"
    assert "Stage stall" not in st.message, st.message
    assert "Engine dead" in st.message, st.message


def test_brief_prints_the_stale_line_not_the_stall_line_when_the_engine_died(
    tmp_git_repo,
):
    """PA-0306-5 on the card: with a stalled LIVE-stage diagnostic AND a
    dead engine, the card prints the STALE line only — never "stall" and
    never a second "engine_dead" wording for the same frozen stage."""
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="card-stale")
    _run_with_a_frozen_stage(proj)

    card = api.brief(proj)

    assert card.stall, "the detector still sees the quiet stamps"
    assert card.engine_dead
    assert card.stale_stage
    assert f"- {card.stale_stage}" in card.text
    assert "- stall:" not in card.text, card.text
    assert "- engine_dead:" not in card.text, card.text


def test_bootstrap_lets_engine_death_outrank_the_stale_advice(tmp_git_repo):
    """TODO-0307 (PA-0306-1): the bootstrap carries BOTH facts, but the
    next step comes from the one advice owner — engine death (resume)
    outranks the frozen-stage wording, never the other way round."""
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="boot-stale")
    _run_with_a_frozen_stage(proj)

    ctx = api.load_supervisor_context(proj)

    assert ctx.engine_dead
    assert ctx.stale_stage
    assert "awf_continue" in ctx.next_action, ctx.next_action
    assert "Stale stage" not in ctx.next_action, ctx.next_action


def test_heartbeat_age_uses_the_shared_parser():
    """AUD-0307-3: the heartbeat's ``ts`` fallback reads ``Z``/naive stamps
    through the same parser as ``updated_at`` instead of its own
    ``datetime.fromisoformat`` (which rejected ``Z`` on older interpreters)."""
    from awf._engine_liveness import _heartbeat_age_seconds

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc).timestamp()
    for raw in ("2026-10-29T11:40:00Z", "2026-10-29T11:40:00+00:00"):
        age = _heartbeat_age_seconds({"ts": raw}, now=now)
        assert age is not None and 1199.9 <= age <= 1200.1, (raw, age)
    assert _heartbeat_age_seconds(
        {"ts": "2026-10-29T11:40:00"}, now=now
    ) == 1200.0
    assert _heartbeat_age_seconds({"ts": "garbage"}, now=now) is None
    assert _heartbeat_age_seconds({"ts_epoch": now - 60}, now=now) == 60.0


def test_python_dash_m_awf_cli_reaches_main():
    """AUD-0307-1: ``python -m awf.cli`` used to import the module and exit
    0 with no output — every "rc 0" from that spelling proved nothing."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    help_run = subprocess.run(
        [sys.executable, "-m", "awf.cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "Usage: awf" in help_run.stdout

    spec_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "awf.cli",
            "spec",
            "check",
            "docs/spec/closer-stage",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert spec_run.returncode == 0, spec_run.stdout + spec_run.stderr
    assert "READY: docs/spec/closer-stage" in spec_run.stdout


def test_bootstrap_drops_do_not_intervene_when_a_live_stage_is_stale(
    tmp_git_repo, monkeypatch
):
    """AR-0307-2 (architect review): the incident shape is a LIVE engine pid
    plus a frozen ``updated_at`` — ``pipeline_running=True``, no engine
    death. The structural answer ("worker stage running … Do NOT
    intervene") must lose to the STALE advice, or the bootstrap tells the
    owner to sit still while nothing moves."""
    from awf import api
    from awf.api import context as context_mod
    from awf.api import lifecycle

    proj = tmp_git_repo
    api.init_project(proj, project_name="live-stale")
    _state(proj, minutes_ago=95)
    log_dir = proj / ".agentic" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = (
        (datetime.now(timezone.utc) - timedelta(minutes=95))
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    (log_dir / "orchestrator.log").write_text(
        f"[{stamp}] Pipeline started (default)\n"
        f"[{stamp}] Stage 1: agent-qa-review (worker :: execute)\n",
        encoding="utf-8",
    )
    # A live engine pid: pipeline_running stays True, engine_dead stays empty —
    # the one combination where the calm structural answer used to win.
    # Both readers ask it: ``get_status`` through ``lifecycle`` and the
    # bootstrap through its own import in ``context``.
    monkeypatch.setattr(
        lifecycle, "check_pipeline_running", lambda _p: (True, os.getpid(), "")
    )
    monkeypatch.setattr(
        context_mod, "check_pipeline_running", lambda _p: (True, os.getpid(), "")
    )

    ctx = api.load_supervisor_context(proj)

    assert ctx.pipeline_running
    assert ctx.stale_stage
    assert not ctx.engine_dead, "a live pid is not an engine death"
    assert "stale" in ctx.next_action.lower(), ctx.next_action
    assert "Do NOT intervene" not in ctx.next_action, ctx.next_action
