"""Closer-stage contract: IMP1 (``skip_if`` in the stage schema, loader,
write path and both snapshots), IMP2 (the decision point, ``dod:``, skip and
static defects) and IMP3 (the stage, its prompt, the snapshot fields and the
rollback of the unit's gate artifacts).

T1 of the closer-stage test spec: the key must survive the per-TODO snapshot
and the service-run snapshot; an unknown value must be refused before any
side effect; an empty ``write_allow`` must not be written; the service run
must drop the stale allowlist of the stage whose output it replaces.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from awf import (
    api,
    checklist_gate,
    closer_stage,
    cmd_checklist,
    paths,
    pipeline_engine,
    todos,
    verify_pack,
)
from awf.api import run as api_run
from awf.api._errors import AwfApiError
from awf.pipeline import (
    Stage,
    load_stages,
    pipeline_snapshot_text,
    stage_to_document,
    validate_pipeline_stages,
)
from awf.pipeline_state import read_state as read_pipeline_state


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: CloserStage\n", encoding="utf-8"
    )
    return proj


def _write_pipeline(proj: Path, name: str, stages: list[dict]) -> Path:
    path = proj / ".agentic" / "pipelines" / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump({"name": name, "stages": stages}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _closer_source() -> list[dict]:
    """Two workers: the first is the closer, the second keeps its fields."""
    return [
        {"name": "plan", "role": "supervisor"},
        {
            "name": "closer",
            "role": "agent-implementer",
            "task": "Закрой пункты",
            "output": "reports/closer.md",
            "write_allow": ["reports/closer-extra.md"],
            "skip_if": "checklist_green",
            "write_check": "off",
        },
        {
            "name": "reviewer",
            "role": "agent-qa-review",
            "task": "Проверь",
            "skip_if": "checklist_green",
            "write_check": "off",
            "output": "reports/review.md",
            "write_allow": ["reports/extra.md"],
        },
        {"name": "verify", "role": "supervisor"},
    ]


def test_skip_if_round_trips_through_both_snapshots(tmp_path):
    proj = _project(tmp_path)
    source = _write_pipeline(proj, "default", _closer_source())

    stages = load_stages(source)
    closer = next(s for s in stages if s.name == "closer")
    assert closer.skip_if == "checklist_green"
    assert next(s for s in stages if s.name == "plan").skip_if == ""

    snapshot_file = proj / ".agentic" / "pipelines" / "PIPELINE-TODO-0001.yaml"
    snapshot_file.write_text(
        pipeline_snapshot_text(stages, "default", "TODO-0001"), encoding="utf-8"
    )
    reloaded = {s.name: s for s in load_stages(snapshot_file)}
    assert reloaded["closer"].skip_if == "checklist_green"
    assert reloaded["reviewer"].skip_if == "checklist_green"
    assert reloaded["closer"].write_check == "off"
    assert reloaded["closer"].write_allow == ("reports/closer-extra.md",)
    assert reloaded["reviewer"].write_allow == ("reports/extra.md",)
    assert reloaded["reviewer"].write_check == "off"

    service_file = api_run._write_service_pipeline(proj, "sla")
    service = {s.name: s for s in load_stages(service_file)}
    # PA1 (TODO-0269): the stage that CREATES the candidate must never skip —
    # its old ``skip_if`` belonged to the replaced output.
    assert service["closer"].skip_if == ""
    assert service["closer"].output == ".agentic/roles/draft/sla.md"
    assert service["closer"].write_allow == ()
    assert service["closer"].write_check == "off"
    assert service["reviewer"].skip_if == "checklist_green"
    assert service["reviewer"].task == "Проверь"
    assert service["reviewer"].output == "reports/review.md"
    assert service["reviewer"].write_allow == ("reports/extra.md",)


def test_unknown_skip_if_value_refused_before_side_effects(tmp_path):
    proj = _project(tmp_path)
    bad_values = ["checklist-grenn", "", 1, True]

    for value in bad_values:
        stages = [
            {"name": "plan", "role": "supervisor"},
            {"name": "closer", "role": "agent-implementer", "skip_if": value},
            {"name": "verify", "role": "supervisor"},
        ]
        with pytest.raises(AwfApiError) as load_err:
            _write_pipeline(proj, "broken", stages)
            load_stages(proj / ".agentic" / "pipelines" / "broken.yaml")
        assert "closer" in str(load_err.value)
        assert repr(value) in str(load_err.value)

        with pytest.raises(AwfApiError) as write_err:
            api.write_pipeline(proj, "bad-write", stages)
        assert "closer" in str(write_err.value)
        assert repr(value) in str(write_err.value)

        with pytest.raises(AwfApiError):
            validate_pipeline_stages(stages, "inline")

        assert not (proj / ".agentic" / "pipelines" / "bad-write.yaml").exists()

    for absent in ({}, {"skip_if": None}):
        stages = [
            {"name": "plan", "role": "supervisor"},
            {"name": "closer", "role": "agent-implementer", **absent},
            {"name": "verify", "role": "supervisor"},
        ]
        loaded = load_stages(_write_pipeline(proj, "plain", stages))
        assert next(s for s in loaded if s.name == "closer").skip_if == ""


def test_empty_write_allow_is_not_written():
    plain = stage_to_document(Stage(name="s", role="worker"))
    assert "write_allow" not in plain
    assert "write_check" not in plain
    assert "skip_if" not in plain

    allowed = stage_to_document(
        Stage(
            name="s",
            role="worker",
            output="out/result.md",
            write_allow=("out/extra.md",),
            write_check="off",
            skip_if="checklist_green",
        )
    )
    assert allowed["write_allow"] == ["out/extra.md"]
    assert allowed["write_check"] == "off"
    assert allowed["skip_if"] == "checklist_green"


# ─── IMP2 (R2): the decision point, ``dod:``, skip and static defects ───


def _closer_project(repo: Path) -> Path:
    """A git repo with ``.agentic/`` ignored and two passing probe targets."""
    (repo / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    for sub in ("inbox", "context", "outbox", "logs"):
        (repo / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    (repo / ".agentic" / "config.yaml").write_text(
        "project:\n  name: CloserStage\n", encoding="utf-8"
    )
    ok = repo / "ok"
    ok.mkdir(exist_ok=True)
    for name in ("a", "b", "c"):
        (ok / name).write_text("ok\n", encoding="utf-8")
    return repo


def _item(item_id: str, target: str) -> str:
    return f'- [ ] {item_id}: {item_id} — probe(file_exists ["{target}"]) expect present'


def _manual(item_id: str) -> str:
    return f"- [ ] {item_id}: {item_id} — manual"


def _write_todo(
    proj: Path,
    todo_id: str,
    *,
    block_items: list[str] | None = None,
    wave: str | None = None,
    raw_block: str | None = None,
    contract: list[str] | None = None,
) -> None:
    parts = [f"# TODO — {todo_id}"]
    if contract is not None:
        parts += ["", "---", *contract, "---"]
    elif wave is not None:
        parts += ["", "---", f"wave: {wave}", "---"]
    if raw_block is not None:
        parts += ["", raw_block]
    elif block_items is not None:
        parts += ["", "## Чек-лист завершения", "", *block_items]
    else:
        parts += ["", "Без чек-листа."]
    (proj / ".agentic" / "inbox" / f"{todo_id}.md").write_text(
        "\n".join(parts) + "\n", encoding="utf-8"
    )


def _closer_stage(**overrides) -> Stage:
    kwargs = {
        "name": "implementer-close",
        "role": "agent-implementer",
        "kind": "execute",
        "task": "Закрой ТОЛЬКО пункты чек-листа",
        "skip_if": "checklist_green",
        "write_check": "off",
    }
    kwargs.update(overrides)
    return Stage(**kwargs)


def _record(proj: Path, todo_id: str) -> dict:
    return json.loads(
        closer_stage.state_path(proj, todo_id).read_text(encoding="utf-8")
    )


def test_decision_writes_one_dod_line_and_skips_without_spawn(tmp_git_repo):
    """T2: the verdict, ``dod:``, the first verdict and the green skip."""
    proj = _closer_project(tmp_git_repo)
    stage = _closer_stage()
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[
            _item("unit-01", "ok/a"),
            _item("unit-02", "ok/nope"),
            _item("unit-03", "ok/b"),
        ],
        wave="w1",
    )

    # The manual surfaces never create the first-verdict history (AC25).
    assert cmd_checklist.run(
        SimpleNamespace(
            action="check",
            todo_id="TODO-0001",
            project_dir=str(proj),
            json=False,
        )
    ) == 1
    verify_pack.verify_pack(
        proj, "TODO-0001", write_report=False, tmp_base=tmp_git_repo.parent
    )
    assert not closer_stage.state_path(proj, "TODO-0001").exists()

    first = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert first.kind == "run"
    assert first.log_line == "dod: 2/3 (missing: unit-02)"
    record = _record(proj, "TODO-0001")
    assert record["wave"] == "w1"
    assert record["first_verdict"]["missing"] == ["unit-02"]
    assert record["attempts"] == 0

    # Same tree and block: the gate cache answers, the history is not rewritten.
    again = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert again.verdict["cached"] is True
    assert _record(proj, "TODO-0001") == record

    # The probe target changed: a fresh run, but still the FIRST verdict.
    (proj / "ok" / "nope").write_text("ok\n", encoding="utf-8")
    changed = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert changed.verdict["cached"] is False
    assert changed.kind == "skip"
    assert changed.log_line == "dod: 3/3"
    assert _record(proj, "TODO-0001")["first_verdict"] == record["first_verdict"]

    # manual items are named but never launch the closer (via ``ok`` or ``run``).
    _write_todo(
        proj,
        "TODO-0001b",
        block_items=[
            _item("unit-01", "ok/a"),
            _item("unit-02", "ok/nope2"),
            _manual("unit-03"),
        ],
    )
    decision = closer_stage.decide(proj, "TODO-0001b", stage, {})
    assert decision.kind == "run"
    assert decision.log_line == "dod: 1/3 (missing: unit-02) (manual: unit-03)"

    _write_todo(
        proj,
        "TODO-0002",
        block_items=[
            _item("g1", "ok/a"),
            _item("g2", "ok/b"),
            _item("g3", "ok/c"),
        ],
        wave="w-green",
    )
    decision = closer_stage.decide(proj, "TODO-0002", stage, {})
    assert decision.kind == "skip"
    assert decision.log_line == "dod: 3/3"
    assert _record(proj, "TODO-0002")["first_verdict"]["ok"] is True
    # K3/AC28: the wave is frozen at the first entry — a green skip records
    # it too, and a later contract rename does not rewrite the record.
    assert _record(proj, "TODO-0002")["wave"] == "w-green"
    _write_todo(
        proj,
        "TODO-0002",
        block_items=[
            _item("g1", "ok/a"),
            _item("g2", "ok/b"),
            _item("g3", "ok/c"),
        ],
        wave="w-renamed",
    )
    closer_stage.decide(proj, "TODO-0002", stage, {})
    assert _record(proj, "TODO-0002")["wave"] == "w-green"

    # No block at all — legacy unit, a skip with ``dod: none``.
    _write_todo(proj, "TODO-0003")
    decision = closer_stage.decide(proj, "TODO-0003", stage, {})
    assert decision.kind == "skip"
    assert decision.log_line == "dod: none"

    # Empty block: a static defect, the owner fixes the block.
    _write_todo(proj, "TODO-0004", block_items=[])
    decision = closer_stage.decide(proj, "TODO-0004", stage, {})
    assert decision.kind == "blocked"
    assert decision.log_line == "dod: 0/0 (missing: (empty block))"
    assert "починить блок" in decision.note
    assert _record(proj, "TODO-0004")["attempts"] == 0

    # Unreadable block: the item that could not be checked is named.
    _write_todo(
        proj,
        "TODO-0005",
        raw_block="## Чек-лист завершения\n\n- [ ] unit-01: broken line — probe\n",
    )
    decision = closer_stage.decide(proj, "TODO-0005", stage, {})
    assert decision.kind == "blocked"
    assert "unit-01" in (decision.verdict.get("missing") or [])

    # Manual-only block: green (``manual`` never launches the closer).
    _write_todo(proj, "TODO-0006", block_items=[_manual("unit-03")])
    decision = closer_stage.decide(proj, "TODO-0006", stage, {})
    assert decision.kind == "skip"
    assert decision.log_line == "dod: 0/1 (manual: unit-03)"


def test_static_block_defect_blocks_without_spawn_or_budget(tmp_git_repo, monkeypatch):
    """T3: an entry hook that skips green, runs red, blocks defects."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[
            _item("g1", "ok/a"),
            _item("g2", "ok/b"),
            _item("g3", "ok/c"),
        ],
    )
    _write_todo(
        proj,
        "TODO-0002",
        block_items=[
            _item("unit-01", "ok/a"),
            _item("unit-02", "ok/nope"),
            _item("unit-03", "ok/c"),
        ],
    )
    _write_todo(proj, "TODO-0004", block_items=[])
    _write_todo(proj, "TODO-0007")

    spawns: list[tuple[str, str]] = []
    gate_calls: list[tuple] = []
    log_lines: list[str] = []

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        spawns.append((stage.name, todo_id))
        root = Path(project_dir)
        # IMP4: a completed closer run is followed by a gate recompute. The
        # closer's job is to close the probe, so the red TODO-0002 turns
        # green and the stage advances to verify (the pre-IMP4 expectation).
        if stage.name == "implementer-close":
            (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# DONE\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    real_gate_run = checklist_gate.run

    def spy_gate(*args, **kwargs):
        gate_calls.append(args)
        return real_gate_run(*args, **kwargs)

    monkeypatch.setattr(checklist_gate, "run", spy_gate)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(
        pipeline_engine, "_log", lambda logs_dir, line: log_lines.append(line)
    )

    stage = _closer_stage()
    plan = Stage(name="plan", role="supervisor", kind="plan")
    verify = Stage(name="verify", role="supervisor", kind="verify")
    stages = [plan, stage, verify]
    logs_dir = proj / ".agentic" / "logs"

    def run(todo_id, used_stage, used_stages, config=None):
        return pipeline_engine.execute_agent_stage(
            stage=used_stage,
            current_todo=todo_id,
            project_dir=proj,
            config=config or {},
            logs_dir=logs_dir,
            stages=used_stages,
            stage_idx=1,
            retry_counts=[0, 0, 0],
            auto=False,
            agent_hard_timeout=None,
            pipeline_name=None,
        )

    # Green: zero spawns, the state says why, the log carries dod and SKIPPED.
    todo, idx, rc = run("TODO-0001", stage, stages)
    assert (todo, idx, rc) == ("TODO-0001", 2, 0)
    assert spawns == []
    state = read_pipeline_state(proj)
    assert state["stage_skipped"] is True
    assert state["stage_skip_reason"] == "checklist_green"
    # The primary guard is ``spawns == []`` (the spawn is mocked); the
    # state assertion documents the contract: a skip records no worker
    # PID. ``pipeline_pid`` belongs to the orchestrator's stage start,
    # which this direct call does not run (DR7).
    assert state.get("worker_pid") is None
    assert any("dod: 3/3" in line for line in log_lines)
    assert any("SKIPPED (checklist_green)" in line for line in log_lines)

    # Red: one closer spawn.
    todo, idx, rc = run("TODO-0002", stage, stages)
    assert spawns == [("implementer-close", "TODO-0002")]
    assert (todo, idx, rc) == ("TODO-0002", 2, 0)

    # A stage without the key runs as before and never calls the gate.
    spawns.clear()
    gate_calls.clear()
    old = Stage(name="worker", role="agent-implementer", kind="execute")
    todo, idx, rc = run("TODO-0007", old, [plan, old, verify])
    assert spawns == [("worker", "TODO-0007")]
    assert gate_calls == []
    # PA2 (TODO-0269): the skip flags are reset at the orchestrator's
    # stage-start write, not here — a direct execute call leaves the state
    # to its caller (see test_next_stage_start_clears_skip_flags).

    # Static stage defect: ``skip_if`` without ``write_check: "off"`` — the
    # gate is not consulted, no worker spawns, the note names the keys.
    spawns.clear()
    gate_calls.clear()
    before_defect = _record(proj, "TODO-0002")
    bad_stage = _closer_stage(write_check="on")
    blocked = proj / ".agentic" / "outbox" / "BLOCKED-TODO-0002.md"
    if blocked.exists():
        blocked.unlink()
    todo, idx, rc = run("TODO-0002", bad_stage, [plan, bad_stage, verify])
    assert rc == 1
    assert idx == 1
    assert spawns == []
    assert gate_calls == []
    note = blocked.read_text(encoding="utf-8")
    assert "implementer-close" in note
    assert "skip_if" in note
    assert "write_check" in note
    assert read_pipeline_state(proj)["last_signal"] == "BLOCKED-TODO-0002"
    assert _record(proj, "TODO-0002") == before_defect
    # The completed DONE run above spent one attempt (IMP4); the static
    # stage defect must NOT spend another.
    assert before_defect["attempts"] == 1

    # Invalid config key: the same refusal, naming the key, with no spawn.
    spawns.clear()
    gate_calls.clear()
    todo, idx, rc = run(
        "TODO-0002", stage, stages, config={"checklist": {"closer_attempts": 3}}
    )
    assert rc == 1
    assert spawns == []
    assert gate_calls == []
    assert "checklist.closer_attempts" in blocked.read_text(encoding="utf-8")

    # Empty block: the gate DOES run (it finds the defect), the closer does not.
    spawns.clear()
    gate_calls.clear()
    todo, idx, rc = run("TODO-0004", stage, stages)
    assert rc == 1
    assert spawns == []
    assert gate_calls != []
    assert _record(proj, "TODO-0004")["attempts"] == 0
    assert read_pipeline_state(proj)["last_signal"] == "BLOCKED-TODO-0004"
    # PA3 through the HOOK (not just ``decide``): the closure pair travels
    # together, so ``is_closed`` / ``blocked_count`` / the event reader see
    # the unit as blocked instead of leaving it active.
    outbox = paths.outbox(proj)
    assert (outbox / "BLOCKED-TODO-0004.md").is_file()
    assert (outbox / "BLOCKED-TODO-0004.ready").is_file()
    assert todos.is_closed(paths.inbox(proj), outbox, "TODO-0004") is True


def test_service_candidate_stage_with_skip_if_still_spawns(
    tmp_git_repo, monkeypatch
):
    """PA1 (TODO-0269) end-to-end: the candidate stage is never skipped.

    The snapshot drops ``skip_if`` from the stage whose ``output`` becomes the
    role candidate. Asserting the snapshot text alone would not prove the
    BEHAVIOR: a green checklist on the service unit must still spawn the one
    worker that creates the candidate — no silent ``dod: none`` skip.
    """
    proj = _closer_project(tmp_git_repo)
    (proj / ".agentic" / "pipelines").mkdir(parents=True, exist_ok=True)
    _write_pipeline(
        proj,
        "default",
        [
            {"name": "plan", "role": "supervisor"},
            {
                "name": "closer",
                "role": "worker",
                "task": "close",
                "output": "reports/old.md",
                "write_check": "off",
                "skip_if": "checklist_green",
            },
            {"name": "verify", "role": "supervisor"},
        ],
    )
    _write_todo(proj, "TODO-0001", block_items=[_item("unit-01", "ok/a")])

    service_file = api_run._write_service_pipeline(proj, "sla")
    stages = load_stages(service_file)
    candidate = next(s for s in stages if s.role.strip().lower() != "supervisor")
    assert candidate.skip_if == ""

    spawns: list[tuple[str, str]] = []
    gate_calls: list[tuple] = []

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        spawns.append((stage.name, todo_id))
        root = Path(project_dir)
        # The worker's contract: the declared output IS the candidate. A
        # missing one would route the stage through its failure path
        # (ORCH M3.2) and hide the skip regression behind a different exit.
        if stage.output:
            target = root / stage.output
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# candidate\n", encoding="utf-8")
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# DONE\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    real_gate_run = checklist_gate.run

    def spy_gate(*args, **kwargs):
        gate_calls.append(args)
        return real_gate_run(*args, **kwargs)

    monkeypatch.setattr(checklist_gate, "run", spy_gate)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    todo, idx, rc = pipeline_engine.execute_agent_stage(
        stage=candidate,
        current_todo="TODO-0001",
        project_dir=proj,
        config={},
        logs_dir=proj / ".agentic" / "logs",
        stages=stages,
        stage_idx=1,
        retry_counts=[0, 0, 0],
        auto=False,
        agent_hard_timeout=None,
        pipeline_name=None,
    )
    assert rc == 0
    assert spawns == [(candidate.name, "TODO-0001")]
    assert gate_calls == []


def test_corrupt_first_verdict_blocks_without_overwriting(tmp_git_repo):
    """K3/AC25: a broken CLOSER record fails closed — no spawn, no rewrite.

    A silent overwrite would lose the first verdict and (in IMP4) the
    attempt counter; the owner must fix the file by hand.
    """
    proj = _closer_project(tmp_git_repo)
    _write_todo(proj, "TODO-0010", block_items=[_item("unit-01", "ok/a")])
    path = closer_stage.state_path(proj, "TODO-0010")

    for broken in ("{broken", "[]"):
        path.write_text(broken, encoding="utf-8")
        decision = closer_stage.decide(proj, "TODO-0010", _closer_stage(), {})
        assert decision.kind == "blocked"
        assert "CLOSER-TODO-0010.json" in decision.note
        assert path.read_text(encoding="utf-8") == broken
        note = (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0010.md").read_text(
            encoding="utf-8"
        )
        assert "CLOSER-TODO-0010.json" in note


def test_static_block_defect_writes_blocked_pair_and_unblocks(tmp_git_repo):
    """PA3 (TODO-0269): the note and the .ready travel together.

    ``is_closed`` / ``blocked_count`` / the event reader key off the marker;
    a note alone left the unit looking active, and ``unblock`` (which moves
    the PAIR) could not clear it.
    """
    proj = _closer_project(tmp_git_repo)
    _write_todo(proj, "TODO-0004", block_items=[])
    decision = closer_stage.decide(proj, "TODO-0004", _closer_stage(), {})
    assert decision.kind == "blocked"

    outbox = paths.outbox(proj)
    assert (outbox / "BLOCKED-TODO-0004.md").is_file()
    assert (outbox / "BLOCKED-TODO-0004.ready").is_file()
    inbox = paths.inbox(proj)
    assert todos.is_closed(inbox, outbox, "TODO-0004") is True

    logs_dir = proj / ".agentic" / "logs"
    assert pipeline_engine._unblock_todo(proj, "TODO-0004", logs_dir) is True
    assert not (outbox / "BLOCKED-TODO-0004.ready").exists()
    assert not (outbox / "BLOCKED-TODO-0004.md").exists()
    assert (paths.context_dir(proj) / "BLOCKED-TODO-0004.ready").is_file()
    assert todos.is_closed(inbox, outbox, "TODO-0004") is False


def test_next_stage_start_clears_skip_flags(tmp_git_repo, monkeypatch):
    """PA2 (TODO-0269): the orchestrator resets the skip flags at stage start.

    A green skip records ``stage_skipped``/``stage_skip_reason``; the next
    stage (a supervisor one included) must not inherit them. The reset lives
    in ONE place — the stage-start write — not in the execute branch.
    """
    from types import SimpleNamespace

    from awf import orchestrator
    from awf.pipeline_state import read_state, write_state

    proj = _closer_project(tmp_git_repo)
    agentic = proj / ".agentic"
    for sub in ("roles", "pipelines", "phases", "state"):
        (agentic / sub).mkdir(parents=True, exist_ok=True)
    (agentic / "config.yaml").write_text(
        "project:\n  name: CloserStage\n"
        'default_pipeline: "default"\n'
        'phases:\n  current: ".agentic/phases/plan.md"\n',
        encoding="utf-8",
    )
    (agentic / "phases" / "plan.md").write_text("- [ ] Step 1\n", encoding="utf-8")
    (agentic / "pipelines" / "default.yaml").write_text(
        "name: default\nstages:\n  - name: plan\n    role: supervisor\n",
        encoding="utf-8",
    )
    write_state(proj, stage_skipped=True, stage_skip_reason="checklist_green")

    seen: dict = {}

    def fake_supervisor(
        stage, todo_id, auto, project_dir, config, logs_dir, pipeline_name=None
    ):
        seen.update(read_state(project_dir) or {})
        return todo_id, 0, 1  # abort after the stage-start write

    monkeypatch.setattr(orchestrator, "execute_supervisor_stage", fake_supervisor)

    args = SimpleNamespace(
        project_dir=str(proj),
        pipeline=None,
        from_stage=None,
        auto=True,
        timeout=None,
        todo_id="",
    )
    assert orchestrator.run_pipeline(args) == 1

    assert seen.get("stage_skipped") is False
    assert seen.get("stage_skip_reason") is None


# ─── IMP3 (R3): the stage, its prompt and the snapshots ───────────────


def test_closer_stage_position_and_prompt(tmp_path):
    """T4: the local ``default`` carries the closer after ``qa-review``.

    The template surfaces (``full`` / ``code-health``) are delivered by IMP7
    (R7); this unit owns the local ``default.yaml`` and the stage contract.
    """
    from awf.agent_stage import _stage_assignment_block

    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / ".agentic" / "pipelines" / "default.yaml"
    if not source.is_file():
        pytest.skip("repo .agentic absent (gitignored in clean CI checkout)")
    stages = load_stages(source)
    names = [s.name for s in stages]

    assert names == [
        "plan",
        "agent-implementer",
        "agent-architect-review",
        "agent-qa-review",
        "checklist-close",
        "checklist-check",
        "agent-project-auditor",
        "verify",
    ]
    idx = names.index("checklist-close")
    assert 0 < idx < len(stages) - 1
    check = stages[names.index("checklist-check")]
    assert check.role == "agent-qa-review"
    assert check.skip_if == "checklist_close_skipped"
    assert check.session_key == "agent-qa-review"
    assert names.index("checklist-check") == idx + 1

    closer = stages[idx]
    assert closer.name == "checklist-close"
    assert closer.id == "checklist-close"
    assert closer.session_key == "agent-implementer"
    assert closer.role == "agent-implementer"
    assert closer.kind == "execute"
    assert closer.skip_if == "checklist_green"
    assert closer.write_check == "off"
    assert closer.output == ""
    assert closer.write_allow == ()

    for phrase in (
        "Закрой ТОЛЬКО пункты",
        "`manual` вне scope",
        "блок не править",
        "`awf checklist check <todo>`",
        "отчитайся пунктами",
    ):
        assert phrase in closer.task

    prompt = _stage_assignment_block(closer)
    assert f"Task: {closer.task}" in prompt
    assert "Expected output:" not in prompt

    snapshot = pipeline_snapshot_text(stages, "default", "TODO-0001")
    reloaded = {s.name: s for s in load_stages(
        _write_text(tmp_path / "PIPELINE-TODO-0001.yaml", snapshot)
    )}
    assert reloaded["checklist-close"].skip_if == "checklist_green"
    assert reloaded["checklist-close"].write_check == "off"
    assert reloaded["checklist-close"].output == ""
    assert reloaded["checklist-close"].write_allow == ()
    assert reloaded["checklist-close"].task == closer.task
    assert reloaded["checklist-check"].skip_if == "checklist_close_skipped"
    assert reloaded["checklist-check"].session_key == "agent-qa-review"
    assert "write_allow" not in yaml.safe_load(snapshot)["stages"][idx]
    assert "output" not in yaml.safe_load(snapshot)["stages"][idx]

    # The service-run snapshot keeps the closer's declared fields; the
    # first worker (agent-implementer) becomes the candidate stage.
    proj = _project(tmp_path)
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    service_file = api_run._write_service_pipeline(proj, "sla")
    service = {s.name: s for s in load_stages(service_file)}
    assert service["checklist-close"].skip_if == "checklist_green"
    assert service["checklist-close"].write_check == "off"
    assert service["checklist-close"].output == ""
    assert service["checklist-close"].write_allow == ()
    assert service["checklist-check"].skip_if == "checklist_close_skipped"
    assert service["agent-implementer"].output == (
        ".agentic/roles/draft/sla.md"
    )


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_stage_error_labels_use_id_when_name_absent(tmp_path):
    """PA3 (audit TODO-0268): a loader error names the stage by its id.

    The canonical closer declares ``name`` AND ``id``; a draft may declare
    only ``id`` — the error must not degrade to a positional ``#1``.
    """
    proj = _project(tmp_path)
    path = _write_pipeline(
        proj,
        "by-id",
        [
            {"name": "plan", "role": "supervisor"},
            {"id": "implementer-close", "role": "worker", "skip_if": "typo"},
            {"name": "verify", "role": "supervisor"},
        ],
    )
    with pytest.raises(AwfApiError) as err:
        load_stages(path)
    assert "'implementer-close'" in str(err.value)


def test_service_rollback_removes_closer_and_checklist_history(tmp_git_repo):
    """AU1 (audit IMP2): a rolled-back service start leaves no unit history.

    ``CLOSER-<id>.json`` and ``CHECKLIST-<id>.json`` belong to the unit id;
    a later unit that reuses the number must not inherit them.
    """
    proj = _closer_project(tmp_git_repo)
    ctx = proj / ".agentic" / "context"
    for name in (
        "CLOSER-TODO-0001.json",
        "CHECKLIST-TODO-0001.json",
        "CLOSER-TODO-0002.json",
        "CHECKLIST-TODO-0002.json",
    ):
        (ctx / name).write_text("{}", encoding="utf-8")

    api_run._rollback_service_side_effects(proj, "TODO-0001", "role-draft-sla")

    assert not (ctx / "CLOSER-TODO-0001.json").exists()
    assert not (ctx / "CHECKLIST-TODO-0001.json").exists()
    assert (ctx / "CLOSER-TODO-0002.json").exists()
    assert (ctx / "CHECKLIST-TODO-0002.json").exists()


# ─── IMP4 (R4): the attempt budget ─────────────────────────────────────


def _closer_stages() -> list:
    plan = Stage(name="plan", role="supervisor", kind="plan")
    verify = Stage(name="verify", role="supervisor", kind="verify")
    return [plan, _closer_stage(), verify]


def _run_closer(proj: Path, todo_id: str, stage: Stage, stages: list, config=None):
    return pipeline_engine.execute_agent_stage(
        stage=stage,
        current_todo=todo_id,
        project_dir=proj,
        config=config or {},
        logs_dir=proj / ".agentic" / "logs",
        stages=stages,
        stage_idx=1,
        retry_counts=[0, 0, 0],
        auto=False,
        agent_hard_timeout=None,
        pipeline_name=None,
    )


def _patch_engine(monkeypatch, spawn) -> None:
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", spawn)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)


def _closer_spawn(proj: Path, spawns: list, *, close: str = "", signal: str = "DONE"):
    """A closer worker: optionally closes one probe, then leaves *signal*."""

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        spawns.append((stage.name, todo_id))
        if close:
            (Path(project_dir) / close).write_text("ok\n", encoding="utf-8")
        outbox = paths.outbox(Path(project_dir))
        (outbox / f"{signal}-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"{signal}-{todo_id}.ready").write_text("", encoding="utf-8")

    return spawn


def test_budget_key_validation_blocks_before_spawn(tmp_git_repo):
    """AC24: only 1 and 2 are budgets; everything else names the key."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(proj, "TODO-0001", block_items=[_item("unit-01", "ok/nope")])
    stage = _closer_stage()

    assert closer_stage.closer_attempts({}) == 1
    for value in (1, 2):
        decision = closer_stage.decide(
            proj, "TODO-0001", stage, {"checklist": {"closer_attempts": value}}
        )
        assert decision.kind == "run"

    record = closer_stage.state_path(proj, "TODO-0001")
    record.unlink(missing_ok=True)

    for value in (0, 3, -1, "2", 2.5, True, "", None):
        decision = closer_stage.decide(
            proj, "TODO-0001", stage, {"checklist": {"closer_attempts": value}}
        )
        assert decision.kind == "blocked", value
        assert "checklist.closer_attempts" in decision.note
        assert repr(value) in decision.note
        assert not record.exists(), f"a refused key wrote the unit record: {value!r}"


def test_budget_limit_one_blocks_after_one_completed_run(
    tmp_git_repo, monkeypatch
):
    """AC10/AC11: the first red closer spends the default budget and blocks."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []
    _patch_engine(monkeypatch, _closer_spawn(proj, spawns))

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert spawns == [("implementer-close", "TODO-0001")]
    assert (todo, idx, rc) == ("TODO-0001", 1, 1)
    assert _record(proj, "TODO-0001")["attempts"] == 1

    note = (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert "unit-02" in note
    assert "решение супервизора" in note

    # A repeat / ``awf continue`` at the cap must not spawn a second closer.
    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert len(spawns) == 1
    assert rc == 1
    assert _record(proj, "TODO-0001")["attempts"] == 1


def test_budget_two_allows_exactly_two_completed_runs(tmp_git_repo, monkeypatch):
    """AC11: budget 2 means the second run happens, then BLOCKED."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []
    _patch_engine(monkeypatch, _closer_spawn(proj, spawns))
    config = {"checklist": {"closer_attempts": 2}}

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 1
    assert (idx, rc) == (1, 0), "red under the cap re-enters the same stage"
    assert _record(proj, "TODO-0001")["attempts"] == 1

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 2
    assert (idx, rc) == (1, 1)
    assert _record(proj, "TODO-0001")["attempts"] == 2

    note = (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert "2/2" in note
    assert "решение супервизора" in note

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 2, "a third closer run is impossible"
    assert rc == 1


def test_budget_green_result_advances_and_freezes_the_first_verdict(
    tmp_git_repo, monkeypatch
):
    """AC10: the result check advances; the first verdict is not rewritten."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        wave="w1",
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []
    _patch_engine(
        monkeypatch, _closer_spawn(proj, spawns, close="ok/nope")
    )

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert (todo, idx, rc) == ("TODO-0001", 2, 0)
    record = _record(proj, "TODO-0001")
    assert record["attempts"] == 1
    assert record["first_verdict"]["missing"] == ["unit-02"]

    # A second entry sees a green tree: skip without a spawn, no rewrite.
    spawns.clear()
    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert spawns == []
    assert (idx, rc) == (2, 0)
    assert _record(proj, "TODO-0001") == record


def test_budget_two_green_on_the_second_run_charges_both(tmp_git_repo, monkeypatch):
    """AC10/AC11: a red retry then a green close spends exactly two runs."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        wave="w1",
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        spawns.append(todo_id)
        # First run leaves the probe red; the second run closes it.
        if len(spawns) == 2:
            (Path(project_dir) / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        outbox = paths.outbox(Path(project_dir))
        (outbox / f"DONE-{todo_id}.md").write_text("# DONE\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    config = {"checklist": {"closer_attempts": 2}}
    first_verdict = None

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 1
    assert (idx, rc) == (1, 0), "red under the cap re-enters the stage"
    record = _record(proj, "TODO-0001")
    assert record["attempts"] == 1
    first_verdict = record["first_verdict"]

    # The retry runs the closer again and this time the result check is green.
    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 2, "the second (last) run happened"
    assert (todo, idx, rc) == ("TODO-0001", 2, 0)
    record = _record(proj, "TODO-0001")
    assert record["attempts"] == 2
    assert record["first_verdict"] == first_verdict, "the first verdict is frozen"


def test_corrupt_attempts_counter_blocks_before_spawn(tmp_git_repo):
    """IMP4: a non-integer counter fails closed — it must not read as zero."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(proj, "TODO-0001", block_items=[_item("unit-01", "ok/nope")])
    path = closer_stage.state_path(proj, "TODO-0001")
    broken = json.dumps({"todo_id": "TODO-0001", "attempts": "2"}) + "\n"
    path.write_text(broken, encoding="utf-8")

    decision = closer_stage.decide(proj, "TODO-0001", _closer_stage(), {})

    assert decision.kind == "blocked"
    assert "invalid attempts counter" in decision.note
    assert path.read_text(encoding="utf-8") == broken, "the record was overwritten"


def test_budget_spends_once_for_every_verdict_signal(tmp_git_repo, monkeypatch):
    """A completed run spends one attempt whatever the signal type."""
    proj = _closer_project(tmp_git_repo)
    stages = _closer_stages()
    stage = stages[1]

    for i, signal in enumerate(("DONE", "REVIEW-APPROVED", "BLOCKED"), start=1):
        todo_id = f"TODO-{i:04d}"
        target = f"ok/s{i}"
        _write_todo(
            proj,
            todo_id,
            block_items=[_item("unit-01", "ok/a"), _item("unit-02", target)],
        )
        spawns: list = []
        _patch_engine(
            monkeypatch,
            _closer_spawn(proj, spawns, close=target, signal=signal),
        )
        todo, idx, rc = _run_closer(proj, todo_id, stage, stages, {})
        assert (idx, rc) == (2, 0), signal
        assert _record(proj, todo_id)["attempts"] == 1, signal


def test_budget_crash_and_salvage_do_not_spend(tmp_git_repo, monkeypatch):
    """AC11: a spawn without a verdict and a salvaged signal cost nothing."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []

    def crash(*args, **kwargs):
        spawns.append(kwargs.get("current_todo"))

    def salvage(stage, todo_id, auto=False, project_dir=".", logs_dir=None, **_kw):
        outbox = paths.outbox(Path(project_dir))
        (outbox / f"DONE-{todo_id}.md").write_text("# salvaged\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, crash)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", salvage)

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert len(spawns) >= 1
    assert (idx, rc) == (1, 1), "a salvaged DONE on a red gate is blocked"
    assert _record(proj, "TODO-0001")["attempts"] == 0, "salvage spent an attempt"

    note = (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert "unit-02" in note
    assert "решение супервизора" in note


def test_salvaged_green_gate_advances_without_spending(tmp_git_repo, monkeypatch):
    """AC11: a salvaged signal on a green tree advances, still for free."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []

    def crash(*args, **kwargs):
        spawns.append(kwargs.get("current_todo"))
        (proj / "ok" / "nope").write_text("ok\n", encoding="utf-8")

    def salvage(stage, todo_id, auto=False, project_dir=".", logs_dir=None, **_kw):
        outbox = paths.outbox(Path(project_dir))
        (outbox / f"DONE-{todo_id}.md").write_text("# salvaged\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, crash)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", salvage)

    todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})
    assert len(spawns) >= 1
    assert (idx, rc) == (2, 0), "the green verdict drives the transition"
    assert _record(proj, "TODO-0001")["attempts"] == 0, "salvage spent an attempt"


# ─── PA-F2/PA-F4: one home for the artifact paths ──────────────────────


def test_unit_history_paths_delegate_to_the_writers(tmp_path, monkeypatch):
    """PA-F2: the cleanup resolves the same paths the writers use."""
    from awf import unit_history

    closer_path = tmp_path / "CUSTOM-CLOSER.json"
    checklist_path = tmp_path / "CUSTOM-CHECKLIST.json"
    monkeypatch.setattr(
        closer_stage, "state_path", lambda project_dir, todo_id: closer_path
    )
    monkeypatch.setattr(
        checklist_gate, "state_path", lambda project, todo_id: checklist_path
    )

    assert unit_history.unit_history_paths(tmp_path, "TODO-0001") == [
        closer_path,
        checklist_path,
    ]

    closer_path.write_text("{}", encoding="utf-8")
    checklist_path.write_text("{}", encoding="utf-8")
    removed = unit_history.forget_unit_history(tmp_path, "TODO-0001")
    assert sorted(removed) == sorted([closer_path.name, checklist_path.name])
    assert not closer_path.exists()
    assert not checklist_path.exists()


def test_force_redispatch_removes_the_closer_path_its_writer_names(
    tmp_git_repo, monkeypatch
):
    """PA-F2: dispatch unlinks the path ``closer_stage.state_path`` names."""
    from awf.api.dispatch import dispatch_todo

    proj = tmp_git_repo
    api.init_project(proj, project_name="PA-F2", test_cmd="/bin/true")
    dispatch_todo(
        proj, "# first\n\nbody.\n", todo_id="TODO-0001", no_baseline_tests=True
    )

    custom = proj / ".agentic" / "context" / "CLOSER-CUSTOM.json"
    custom.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        closer_stage, "state_path", lambda project_dir, todo_id: custom
    )

    dispatch_todo(
        proj,
        "# second\n\nbody.\n",
        todo_id="TODO-0001",
        force=True,
        no_baseline_tests=True,
    )
    assert not custom.exists()


def test_pipeline_snapshot_path_is_one_accessor(tmp_path, monkeypatch):
    """PA-F4: orchestrator, supervisor and budget read the same accessor."""
    from awf import budget, orchestrator, supervisor

    sentinel = tmp_path / "PIPELINE-TODO-0001.yaml"
    seen: list = []

    def fake(project_dir=".", todo_id=""):
        seen.append((Path(project_dir), todo_id))
        return sentinel

    monkeypatch.setattr(paths, "pipeline_snapshot_path", fake)

    assert orchestrator._pipeline_snapshot_file(tmp_path, "TODO-0001") == sentinel
    assert supervisor._snapshot_stage_names(tmp_path, "TODO-0001") is None
    assert budget._unit_segments(tmp_path, "TODO-0001") == []
    assert seen == [(tmp_path, "TODO-0001")] * 3


# ─── IMP5 (R5): block and contract immutability ────────────────────────


_NORM_CONTRACT = (
    'verify: ["python -m pytest -q"]',
    'gates: ["contracts"]',
    'prove_red: ["tests/x.py::test_y"]',
    'files: ["src/x.py"]',
)


def _todo_path(proj: Path, todo_id: str) -> Path:
    return proj / ".agentic" / "inbox" / f"{todo_id}.md"


def _edit_todo(proj: Path, todo_id: str, *, old: str, new: str) -> None:
    path = _todo_path(proj, todo_id)
    text = path.read_text(encoding="utf-8")
    assert old in text, f"edit target missing: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _read_blocked_note(proj: Path, todo_id: str) -> str:
    return (
        proj / ".agentic" / "outbox" / f"BLOCKED-{todo_id}.md"
    ).read_text(encoding="utf-8")


def test_norm_snapshot_is_taken_once_and_survives_restart(tmp_git_repo):
    """AC14: ``before`` is the original snapshot, not the previous attempt."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        contract=list(_NORM_CONTRACT),
    )
    stage = _closer_stage()

    first = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert first.kind == "run"
    record = _record(proj, "TODO-0001")
    before = record["before"]
    assert before["block_sha"]
    assert before["contract_sha"] == closer_stage.contract_fingerprint(
        {
            "verify": ["python -m pytest -q"],
            "gates": ["contracts"],
            "prove_red": ["tests/x.py::test_y"],
            "files": ["src/x.py"],
        }
    )

    # A restart is just another decide: the file is the only memory.
    again = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert again.kind == "run"
    assert _record(proj, "TODO-0001")["before"] == before

    # ``files`` is a report, not a norm: its edit is not a violation.
    _edit_todo(
        proj,
        "TODO-0001",
        old='files: ["src/x.py"]',
        new='files: ["src/y.py"]',
    )
    files_edit = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert files_edit.kind == "run"
    assert _record(proj, "TODO-0001")["before"] == before

    # A block edit is refused on the NEXT entry, before any spawn.
    _edit_todo(
        proj, "TODO-0001", old="- [ ] unit-01:", new="- [x] unit-01:"
    )
    blocked = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert blocked.kind == "blocked"
    assert "closer edited the checklist/contract" in blocked.note
    assert "Чек-лист завершения" in blocked.note
    assert _record(proj, "TODO-0001")["before"] == before


def test_norm_snapshot_survives_a_second_closer_run(tmp_git_repo, monkeypatch):
    """AC14: a repeat closer compares with the ORIGINAL snapshot."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        contract=list(_NORM_CONTRACT),
    )
    stages = _closer_stages()
    stage = stages[1]
    spawns: list = []

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        spawns.append(todo_id)
        root = Path(project_dir)
        if len(spawns) == 2:
            (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
            path = _todo_path(root, todo_id)
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\n"
                + _manual("unit-99")
                + "\n",
                encoding="utf-8",
            )
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    config = {"checklist": {"closer_attempts": 2}}

    _todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert (idx, rc) == (1, 0), "a red first run re-enters the stage"
    after_first = _record(proj, "TODO-0001")["before"]
    assert _record(proj, "TODO-0001")["attempts"] == 1

    _todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, config)
    assert len(spawns) == 2, "the second (last) run happened"
    assert (idx, rc) == (1, 1), "the edit in the second run still blocks"
    record = _record(proj, "TODO-0001")
    assert record["before"] == after_first, "the snapshot is the original one"
    assert record["attempts"] == 2
    note = _read_blocked_note(proj, "TODO-0001")
    assert "closer edited the checklist/contract" in note
    assert "Чек-лист завершения" in note


def test_closer_block_edit_blocks_even_when_items_closed(
    tmp_git_repo, monkeypatch
):
    """AC12: a green gate does not cancel an edited block."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        contract=list(_NORM_CONTRACT),
    )
    stages = _closer_stages()
    stage = stages[1]

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        root = Path(project_dir)
        (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        path = _todo_path(root, todo_id)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + _manual("unit-99") + "\n",
            encoding="utf-8",
        )
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    _todo, _idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})

    assert rc == 1
    note = _read_blocked_note(proj, "TODO-0001")
    assert "closer edited the checklist/contract" in note
    assert "Чек-лист завершения" in note
    # Doctrine 02: the edit stays in the tree; the attempt was charged
    # before the colour decision, so a norm edit cannot buy a free retry.
    assert "unit-99" in _todo_path(proj, "TODO-0001").read_text(encoding="utf-8")
    assert _record(proj, "TODO-0001")["attempts"] == 1


@pytest.mark.parametrize(
    ("old", "new", "blocked"),
    [
        (
            'verify: ["python -m pytest -q"]',
            'verify: ["python -m pytest -x"]',
            True,
        ),
        ('gates: ["contracts"]', 'gates: ["ratchet"]', True),
        (
            'prove_red: ["tests/x.py::test_y"]',
            'prove_red: ["tests/z.py::test_y"]',
            True,
        ),
        ('files: ["src/x.py"]', 'files: ["src/y.py"]', False),
    ],
)
def test_closer_contract_edit_blocks_and_files_edit_passes(
    tmp_git_repo, monkeypatch, old, new, blocked
):
    """AC13: the frozen keys are a norm; ``files`` is a report."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        contract=list(_NORM_CONTRACT),
    )
    stages = _closer_stages()
    stage = stages[1]

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        root = Path(project_dir)
        (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        _edit_todo(root, todo_id, old=old, new=new)
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    _todo, idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})

    if blocked:
        assert (idx, rc) == (1, 1)
        note = _read_blocked_note(proj, "TODO-0001")
        assert "closer edited the checklist/contract" in note
        assert "контракт TODO" in note
    else:
        assert (idx, rc) == (2, 0)
        assert not (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").exists()
    assert _record(proj, "TODO-0001")["attempts"] == 1


def test_unreadable_contract_after_closer_blocks(tmp_git_repo, monkeypatch):
    """K5: a contract that no longer parses is the same refusal."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        contract=list(_NORM_CONTRACT),
    )
    stages = _closer_stages()
    stage = stages[1]

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        root = Path(project_dir)
        (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        _edit_todo(
            root,
            todo_id,
            old='verify: ["python -m pytest -q"]',
            new='verify: ["python -m pytest -q"',
        )
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    _todo, _idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})

    assert rc == 1
    note = _read_blocked_note(proj, "TODO-0001")
    assert "closer edited the checklist/contract" in note
    assert "контракт TODO" in note


def test_norm_compare_survives_absent_contract(tmp_git_repo):
    """AC13/T6: no contract — the comparison does not crash; an edit is caught."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stage = _closer_stage()

    assert closer_stage.decide(proj, "TODO-0001", stage, {}).kind == "run"
    before = _record(proj, "TODO-0001")["before"]
    assert before["contract_sha"] == closer_stage.contract_fingerprint(None)

    # Close the missing probe AND edit the block: the gate is green, the norm
    # edit still refuses (closed items do not cancel the refusal).
    (proj / "ok" / "nope").write_text("ok\n", encoding="utf-8")
    path = _todo_path(proj, "TODO-0001")
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + _manual("unit-99") + "\n",
        encoding="utf-8",
    )
    blocked = closer_stage.decide(proj, "TODO-0001", stage, {})
    assert blocked.kind == "blocked"
    assert "closer edited the checklist/contract" in blocked.note
    assert "Чек-лист завершения" in blocked.note
    assert _record(proj, "TODO-0001")["before"] == before


def test_missing_record_after_run_fails_closed(tmp_git_repo, monkeypatch):
    """CTX1 (audit IMP4): a deleted record cannot buy a fresh budget.

    ``settle_run`` re-reads the record AFTER the run; a missing file is a
    closed refusal, not a fresh ``attempts=0``. The residual threat — an
    operator deleting the file and starting a new ``decide`` — is outside
    the model (the record is the unit's only memory).
    """
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]

    def spawn(stage, todo_id, project_dir, config, logs_dir, **kw):
        root = Path(project_dir)
        (root / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        closer_stage.state_path(root, todo_id).unlink()
        outbox = paths.outbox(root)
        (outbox / f"DONE-{todo_id}.md").write_text("# signal\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, spawn)
    _todo, _idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})

    assert rc == 1
    note = _read_blocked_note(proj, "TODO-0001")
    assert "CLOSER-TODO-0001.json" in note
    assert "потерян" in note


def test_salvaged_norm_edit_blocks_without_spending(tmp_git_repo, monkeypatch):
    """K5 + AC11: an edit by a crashed closer is still a violation; free."""
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
    )
    stages = _closer_stages()
    stage = stages[1]

    def crash(*args, **kwargs):
        (proj / "ok" / "nope").write_text("ok\n", encoding="utf-8")
        path = _todo_path(proj, "TODO-0001")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n" + _manual("unit-99") + "\n",
            encoding="utf-8",
        )

    def salvage(stage, todo_id, auto=False, project_dir=".", logs_dir=None, **_kw):
        outbox = paths.outbox(Path(project_dir))
        (outbox / f"DONE-{todo_id}.md").write_text("# salvaged\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    _patch_engine(monkeypatch, crash)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", salvage)

    _todo, _idx, rc = _run_closer(proj, "TODO-0001", stage, stages, {})

    assert rc == 1
    note = _read_blocked_note(proj, "TODO-0001")
    assert "closer edited the checklist/contract" in note
    assert "Чек-лист завершения" in note
    assert _record(proj, "TODO-0001")["attempts"] == 0, "salvage spent an attempt"


# ─── IMP6 (R6): `awf checklist stats` ─────────────────────────────────


def _closer_record(
    proj: Path, todo_id: str, items: list[dict], *, wave: str = ""
) -> Path:
    """A record exactly as IMP4 writes it on the first entry of a unit."""
    path = proj / ".agentic" / "context" / f"CLOSER-{todo_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "todo_id": todo_id,
                "wave": wave,
                "first_verdict": {
                    "status": "checked",
                    "ok": False,
                    "items": items,
                    "missing": [],
                    "manual": [],
                    "tree_sha": "t",
                    "block_sha": "b",
                    "probe_sha": "p",
                },
                "attempts": 1,
            }
        ),
        encoding="utf-8",
    )
    return path


def _stats(proj: Path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    from awf.cli import _build_parser

    parser, _sub = _build_parser()
    args = parser.parse_args(
        ["checklist", "stats", *extra, "--project-dir", str(proj)]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_checklist.run(args)
    return rc, buf.getvalue()


def _stats_items(unit: str, status: str = "ok") -> list[dict]:
    return [{"id": unit, "status": status, "detail": ""}]


def test_stats_first_verdict_and_wave_filter(tmp_path):
    """T7/AC15–AC17, AC28: first verdicts only, no manual, no invented zeros."""
    proj = _project(tmp_path)
    # A — red on the first verdict (the unit is then closed by the closer);
    # B — green at once; C — manual only; D — another unit on the item A
    # carried; one unit without a wave.
    _closer_record(
        proj,
        "TODO-0001",
        _stats_items("unit-01", "missing") + _stats_items("unit-manual", "manual"),
        wave="w-a",
    )
    _closer_record(proj, "TODO-0002", _stats_items("unit-02"), wave="w-a")
    _closer_record(proj, "TODO-0003", _stats_items("unit-manual", "manual"), wave="w-b")
    _closer_record(proj, "TODO-0004", _stats_items("unit-01"), wave="w-b")
    _closer_record(proj, "TODO-0005", _stats_items("unit-03"))

    rc, out = _stats(proj)
    lines = out.splitlines()
    assert rc == 0
    assert lines[0] == "| Пункт | caught | runs |"
    assert set(lines[1]) <= {"|", "-", " "}
    assert "unit-01 | 1 | 2" in out
    assert "unit-02 | 0 | 1" in out
    assert "unit-03 | 0 | 1" in out
    assert "unit-manual" not in out

    rc, out = _stats(proj, "--wave", "w-a")
    rows = [line for line in out.splitlines()[2:] if line.strip()]
    assert rc == 0
    assert any("unit-01 | 1 | 1" in line for line in rows)
    assert any("unit-02 | 0 | 1" in line for line in rows)
    assert not any("unit-03" in line for line in rows)

    rc, out = _stats(proj, "--wave", "w-zzz")
    assert rc == 0
    assert out.splitlines() == ["| Пункт | caught | runs |", "| --- | --- | --- |"]

    rc, out = _stats(proj, "--json")
    rows = json.loads(out)
    assert rc == 0
    assert {"item": "unit-01", "caught": 1, "runs": 2} in rows
    assert {"item": "unit-02", "caught": 0, "runs": 1} in rows
    assert all(set(row) == {"item", "caught", "runs"} for row in rows)
    assert not any(row["item"] == "unit-manual" for row in rows)

    # A tree change with a rewritten verdict cache does not move the
    # history: the record is written once per unit.
    (proj / ".agentic" / "context" / "CHECKLIST-TODO-0001.json").write_text(
        json.dumps({"tree_sha": "other", "ok": True}), encoding="utf-8"
    )
    rc, again = _stats(proj, "--json")
    assert rc == 0
    assert json.loads(again) == rows

    # A unit without a wave never matches a wave filter; a wave file renamed
    # after dispatch changes nothing (the record keeps the frozen name).
    rc, out = _stats(proj, "--wave", "w-b", "--json")
    assert rc == 0
    assert [row["item"] for row in json.loads(out)] == ["unit-01"]
    waves = proj / ".agentic" / "checklists" / "waves"
    waves.mkdir(parents=True, exist_ok=True)
    (waves / "w-b.yaml").write_text("name: w-b\nitems: []\n", encoding="utf-8")
    (waves / "w-b.yaml").rename(waves / "w-renamed.yaml")
    rc, out = _stats(proj, "--wave", "w-b", "--json")
    assert rc == 0
    assert [row["item"] for row in json.loads(out)] == ["unit-01"]


def test_stats_empty_project_and_broken_record(tmp_path, capsys):
    """AC17: no data — the header only, rc 0; a broken record is skipped."""
    proj = _project(tmp_path)
    assert _stats(proj) == (0, "| Пункт | caught | runs |\n| --- | --- | --- |\n")
    assert _stats(proj, "--json") == (0, "[]\n")
    # A reader, not a dispatch: no context directory is created for the scan.
    assert not (proj / ".agentic" / "context").exists()

    path = proj / ".agentic" / "context" / "CLOSER-TODO-0001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    rc, out = _stats(proj)
    assert rc == 0
    assert out.splitlines()[0] == "| Пункт | caught | runs |"
    assert len([line for line in out.splitlines() if line.strip()]) == 2
    # The warning goes to stderr, so the machine-readable stream stays JSON.
    rc, out = _stats(proj, "--json")
    assert (rc, json.loads(out)) == (0, [])
    assert "CLOSER-TODO-0001.json" in capsys.readouterr().err


def test_stats_is_a_live_subcommand_and_unknown_commands_refuse(tmp_path):
    """cli-parity: the command list comes from the live subparsers.

    ``stats`` needs no TODO id; the old required positional would make the
    new action unusable from the shell.
    """
    import contextlib
    import io

    from awf import cli

    parser, sub = cli._build_parser()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cli._print_top_level_help(sub) == 0
    assert "checklist" in buf.getvalue()

    args = parser.parse_args(["checklist", "stats"])
    assert (args.action, args.todo_id, args.wave) == ("stats", None, "")
    args = parser.parse_args(["checklist", "stats", "--wave", "w-a", "--json"])
    assert (args.wave, args.json) == ("w-a", True)

    with pytest.raises(SystemExit):
        parser.parse_args(["no-such-command"])


def test_stats_reads_the_writer_record_end_to_end(tmp_git_repo):
    """AC15: the statistics is bound to the WRITER's record shape.

    The fixtures above spell the record by hand; a rename inside
    ``_verdict_facts`` would keep them green and leave ``stats`` silently
    empty. Here the record comes from ``closer_stage.decide`` itself: a red
    first verdict, then a green re-check after the closer closed the gap.
    """
    proj = _closer_project(tmp_git_repo)
    _write_todo(
        proj,
        "TODO-0001",
        block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
        wave="w1",
    )
    stage = _closer_stage()
    assert closer_stage.decide(proj, "TODO-0001", stage, {}).kind == "run"

    rc, out = _stats(proj)
    assert rc == 0
    assert "| unit-01 | 0 | 1 |" in out
    assert "| unit-02 | 1 | 1 |" in out
    # The wave filter reads the field frozen into the record at dispatch.
    assert _stats(proj, "--wave", "w1") == (0, out)

    # The closer closes the gap: the post-run re-check is green and charges
    # an attempt, but the first verdict — and so the counters — stays as it
    # was (AC15).
    (proj / "ok" / "nope").write_text("ok\n", encoding="utf-8")
    assert closer_stage.settle_run(proj, "TODO-0001", {}).kind == "green"
    assert _stats(proj) == (0, out)


# ─── IMP7 (R7/R8): delivery — generator, templates, init, old pipeline ──


def test_generator_and_templates_carry_closer(tmp_git_repo):
    """T8: the generator and both shipped templates deliver the closer.

    AC18 — a project composed from the form gets ``implementer-close``
    after the last writing stage (``qa-review``) and before the
    findings-only auditor; ``full`` and ``code-health`` carry it in place
    and load through the shared validator. AC19 — ``awf init`` writes no
    pipeline and leaves an existing file byte-for-byte; an old pipeline
    without the key loads with an empty ``skip_if``.
    """
    from awf.api import setup

    team = [
        {"agent": "agent-implementer"},
        {"agent": "agent-architect-review"},
        {"agent": "agent-qa-review"},
        {"agent": "agent-project-auditor"},
    ]
    generated = setup.build_pipeline_stages(team)
    assert [s["name"] for s in generated] == [
        "plan",
        "agent-implementer",
        "agent-architect-review",
        "agent-qa-review",
        "checklist-close",
        "checklist-check",
        "agent-project-auditor",
        "verify",
    ]

    for variant, before in (
        ("full", "agent-qa-review"),
        ("code-health", "agent-refactoring-specialist"),
    ):
        template = setup._builtin_pipeline_template(variant)
        assert template is not None, variant
        stages = load_stages(template)
        names = [s.name for s in stages]
        assert "checklist-close" in names, variant
        index = names.index("checklist-close")
        assert names[index - 1] == before, (variant, names)
        # TODO-0304: the follow-up check sits right after the close.
        assert names[index + 1] == "checklist-check", (variant, names)
        check = stages[index + 1]
        assert check.role == "agent-qa-review"
        assert check.skip_if == "checklist_close_skipped"
        closer = stages[index]
        assert closer.kind == "execute"
        assert closer.skip_if == "checklist_green"
        assert closer.write_check == "off"
        assert closer.output == "" and closer.write_allow == ()
        generated_closer = next(
            stage for stage in generated if stage["name"] == "checklist-close"
        )
        assert closer.task == generated_closer["task"], (
            f"{variant}: the template task drifted from the generator"
        )

    # AC19: init creates the directory but no pipeline, and re-init is
    # non-destructive — an existing file keeps its bytes.
    api.init_project(tmp_git_repo, project_name="CloserDelivery")
    pipelines = tmp_git_repo / ".agentic" / "pipelines"
    assert not list(pipelines.glob("*.yaml"))
    old = pipelines / "default.yaml"
    old.write_text(
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n"
        "- name: worker\n  role: worker\n"
        "- name: verify\n  role: supervisor\n",
        encoding="utf-8",
    )
    before_bytes = old.read_bytes()
    api.init_project(tmp_git_repo, project_name="CloserDelivery")
    assert old.read_bytes() == before_bytes
    assert all(stage.skip_if == "" for stage in load_stages(old))


def test_unit_contract_documents_the_closer_surface():
    """T8 (audit IMP4/IMP6): the owner-facing document names the knobs."""
    contract = (
        Path(__file__).resolve().parents[2] / "docs" / "unit-contract.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "skip_if",
        "checklist.closer_attempts",
        "awf checklist stats",
        "CLOSER-<todo>.json",
    ):
        assert phrase in contract, phrase


# ─── TODO-0298: the allowances are part of the frozen norm ───────────────


class TestFrozenAllowances:
    BASE = {
        "verify": ["python -m pytest -q"],
        "gates": ["contracts"],
        "prove_red": ["tests/x.py::test_y"],
    }

    def test_adding_an_allowance_moves_the_hash(self):
        before = closer_stage.contract_fingerprint(dict(self.BASE))
        for key in ("xfail_allow", "skip_allow"):
            with_allow = dict(self.BASE)
            with_allow[key] = 1
            assert closer_stage.contract_fingerprint(with_allow) != before, key

    def test_key_order_and_absent_or_zero_are_stable(self):
        ordered = dict(self.BASE)
        reordered = {k: ordered[k] for k in reversed(list(ordered))}
        assert (
            closer_stage.contract_fingerprint(ordered)
            == closer_stage.contract_fingerprint(reordered)
        )
        assert closer_stage.contract_fingerprint(None) == (
            closer_stage.contract_fingerprint({})
        )
        explicit_zero = dict(ordered, xfail_allow=0, skip_allow=0)
        assert closer_stage.contract_fingerprint(explicit_zero) == (
            closer_stage.contract_fingerprint(ordered)
        )

    def test_adding_an_allowance_after_the_snapshot_is_a_norm_edit(
        self, tmp_git_repo
    ):
        proj = _closer_project(tmp_git_repo)
        _write_todo(
            proj,
            "TODO-0001",
            block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
            contract=list(_NORM_CONTRACT),
        )
        stage = _closer_stage()
        assert closer_stage.decide(proj, "TODO-0001", stage, {}).kind == "run"

        _edit_todo(
            proj,
            "TODO-0001",
            old='files: ["src/x.py"]',
            new='files: ["src/x.py"]\nxfail_allow: 1',
        )
        blocked = closer_stage.decide(proj, "TODO-0001", stage, {})
        assert blocked.kind == "blocked"

    def test_raising_a_declared_zero_after_the_snapshot_is_a_norm_edit(
        self, tmp_git_repo
    ):
        """A declared 0 is the norm; raising it to 1 is still an edit."""
        proj = _closer_project(tmp_git_repo)
        _write_todo(
            proj,
            "TODO-0001",
            block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
            contract=list(_NORM_CONTRACT) + ["xfail_allow: 0"],
        )
        stage = _closer_stage()
        assert closer_stage.decide(proj, "TODO-0001", stage, {}).kind == "run"

        _edit_todo(proj, "TODO-0001", old="xfail_allow: 0", new="xfail_allow: 1")
        blocked = closer_stage.decide(proj, "TODO-0001", stage, {})
        assert blocked.kind == "blocked"
        assert "контракт TODO" in (blocked.log_line or "")

    def test_the_two_allowances_do_not_collapse_into_one(self):
        xfail = closer_stage.contract_fingerprint(dict(self.BASE, xfail_allow=1))
        skip = closer_stage.contract_fingerprint(dict(self.BASE, skip_allow=1))
        assert xfail != skip


class TestFrozenProveRedDetails:
    """AUD-0298-1: the prove-red mechanism is part of the frozen norm."""

    BASE = {
        "verify": ["python -m pytest -q"],
        "gates": ["contracts"],
        "prove_red": ["tests/x.py::test_y"],
    }

    def _with_details(self, **overrides):
        contract = dict(
            self.BASE,
            prove_red_kind="coverage",
            prove_red_note="the behavior predates this unit",
        )
        contract.update(overrides)
        return contract

    def test_switching_the_kind_moves_the_hash(self):
        """The note is held equal, so only the kind can move the hash."""
        coverage = self._with_details()
        new_api = self._with_details(prove_red_kind="new-api")
        assert closer_stage.contract_fingerprint(new_api) != (
            closer_stage.contract_fingerprint(coverage)
        )

    def test_switching_the_note_moves_the_hash(self):
        one = self._with_details(prove_red_note="one")
        two = self._with_details(prove_red_note="two")
        assert closer_stage.contract_fingerprint(two) != (
            closer_stage.contract_fingerprint(one)
        )

    def test_switching_the_command_moves_the_hash(self):
        """Two different commands must not collapse into one hash."""
        pytest_cmd = dict(self.BASE, prove_red_cmd="pytest tests/x.py -q")
        npm_cmd = dict(self.BASE, prove_red_cmd="npm test -- --run")
        assert closer_stage.contract_fingerprint(npm_cmd) != (
            closer_stage.contract_fingerprint(pytest_cmd)
        )

    def test_the_details_stay_out_of_the_absent_zero_normalisation(self):
        """Only the allowance keys normalise: an empty detail string is a value."""
        bare = closer_stage.contract_fingerprint(dict(self.BASE))
        empty_kind = closer_stage.contract_fingerprint(
            dict(self.BASE, prove_red_kind="")
        )
        assert empty_kind != bare

    def test_switching_the_kind_after_the_snapshot_is_a_norm_edit(
        self, tmp_git_repo
    ):
        """AUD-0298-1 at the guard: a closer round cannot declare `coverage`.

        `prove_red_kind: coverage` turns a not-red baseline into a pass in
        verify-pack, so the unit could hide "the tests prove nothing" by
        editing the contract after the norm snapshot.
        """
        proj = _closer_project(tmp_git_repo)
        _write_todo(
            proj,
            "TODO-0001",
            block_items=[_item("unit-01", "ok/a"), _item("unit-02", "ok/nope")],
            contract=list(_NORM_CONTRACT)
            + [
                "prove_red_kind: new-api",
                'prove_red_note: "the symbol is new in this unit"',
            ],
        )
        stage = _closer_stage()
        assert closer_stage.decide(proj, "TODO-0001", stage, {}).kind == "run"

        _edit_todo(
            proj,
            "TODO-0001",
            old="prove_red_kind: new-api",
            new="prove_red_kind: coverage",
        )
        blocked = closer_stage.decide(proj, "TODO-0001", stage, {})
        assert blocked.kind == "blocked"
        assert "контракт TODO" in (blocked.log_line or "")


# ─── AUD-0299-1: flake and prove_red_pin are part of the frozen norm ──────


class TestFrozenFlakeAndPin:
    """AUD-0299-1: both keys decide what the unit is checked against."""

    BASE = {
        "verify": ["python -m pytest -q"],
        "gates": ["contracts"],
        "prove_red": ["tests/x.py::test_y"],
    }

    def test_switching_flake_moves_the_hash(self):
        on = closer_stage.contract_fingerprint(dict(self.BASE, flake="on"))
        off = closer_stage.contract_fingerprint(dict(self.BASE, flake="off"))
        assert on != off

    def test_switching_the_pin_list_moves_the_hash(self):
        one = closer_stage.contract_fingerprint(
            dict(self.BASE, prove_red_pin=["tests/x.py::test_y"])
        )
        two = closer_stage.contract_fingerprint(
            dict(self.BASE, prove_red_pin=["tests/x.py::test_z"])
        )
        assert one != two

    def test_key_order_and_an_empty_contract_stay_stable(self):
        ordered = dict(
            self.BASE,
            flake="off",
            prove_red_pin=["tests/x.py::test_y"],
        )
        reordered = {k: ordered[k] for k in reversed(list(ordered))}
        assert closer_stage.contract_fingerprint(ordered) == (
            closer_stage.contract_fingerprint(reordered)
        )
        assert closer_stage.contract_fingerprint(None) == (
            closer_stage.contract_fingerprint({})
        )
