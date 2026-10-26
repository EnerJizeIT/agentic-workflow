"""IMP1 (R1): ``skip_if`` in the stage schema, loader, write path and both
pipeline snapshots.

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
) -> None:
    parts = [f"# TODO — {todo_id}"]
    if wave is not None:
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
        outbox = paths.outbox(Path(project_dir))
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
    assert before_defect["attempts"] == 0

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
