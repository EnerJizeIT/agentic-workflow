"""A27: ``DONE-<id>.json`` belongs to the author of the report.

Every pipeline stage used to overwrite the unit's DONE json, so an auditor's
file replaced the implementer's facts. The engine now restores a foreign
overwrite and keeps the reader's claims in ``PROBES-<id>.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import done_owner


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("outbox", "inbox", "logs", "context", "roles"):
        (project / ".agentic" / name).mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  agent-implementer: execute\n", encoding="utf-8"
    )
    (project / ".agentic" / "roles" / "agent-implementer.md").write_text(
        "# Role\n", encoding="utf-8"
    )
    return project


def _outbox(project: Path) -> Path:
    return project / ".agentic" / "outbox"


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _author(project: Path, tests_run: list[dict], stage: str = "agent-implementer") -> Path:
    path = _outbox(project) / "DONE-TODO-0001.json"
    _write(path, {"tests_run": tests_run, "stage": {"stage_id": stage, "attempt": 1}})
    return path


def test_an_auditor_cannot_rewrite_the_authors_tests_run(tmp_path):
    """The auditor's own file is PROBES; DONE keeps the author's commands."""
    project = _project(tmp_path)
    author_tests = [{"cmd": "python3 -m pytest tests/unit/test_x.py -q", "result": "12 passed"}]
    done_path = _author(project, author_tests)
    before = json.loads(done_path.read_text(encoding="utf-8"))["tests_run"]

    snapshot = done_owner.snapshot(_outbox(project), "TODO-0001")
    _write(
        done_path,
        {
            "tests_run": [{"cmd": "reused canonical suite artifact x.json", "result": "exit 0"}],
            "stage": {"stage_id": "agent-project-auditor", "attempt": 1},
        },
    )
    _write(
        _outbox(project) / "PROBES-TODO-0001.json",
        {"tests_run": [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}]},
    )

    restored = done_owner.restore(
        _outbox(project), "TODO-0001",
        stage_id="agent-project-auditor", snapshot_text=snapshot,
    )

    assert restored is True
    after = json.loads(done_path.read_text(encoding="utf-8"))["tests_run"]
    assert after == before
    assert json.loads((_outbox(project) / "PROBES-TODO-0001.json").read_text())["tests_run"]


def test_a_foreign_overwrite_without_probes_is_kept_as_probes(tmp_path):
    """The reader's facts are not lost: they move to the file that owns them."""
    project = _project(tmp_path)
    done_path = _author(project, [{"cmd": "true", "result": "ok"}])

    snapshot = done_owner.snapshot(_outbox(project), "TODO-0001")
    auditor_facts = {"tests_run": [{"cmd": "python3 -c 'print(2)'", "result": "1 passed"}]}
    _write(done_path, {**auditor_facts, "stage": {"stage_id": "agent-project-auditor"}})

    assert done_owner.restore(
        _outbox(project), "TODO-0001",
        stage_id="agent-project-auditor", snapshot_text=snapshot,
    )

    probes = _outbox(project) / "PROBES-TODO-0001.json"
    assert json.loads(probes.read_text(encoding="utf-8"))["tests_run"] == auditor_facts["tests_run"]


def test_the_owner_stage_may_rewrite_its_own_facts(tmp_path):
    """A retry of the same stage updates DONE; the restore stays out of it."""
    project = _project(tmp_path)
    done_path = _author(project, [{"cmd": "true", "result": "ok"}])

    snapshot = done_owner.snapshot(_outbox(project), "TODO-0001")
    newer = [{"cmd": "true", "result": "2 passed"}]
    _write(done_path, {"tests_run": newer, "stage": {"stage_id": "agent-implementer", "attempt": 2}})

    assert done_owner.restore(
        _outbox(project), "TODO-0001",
        stage_id="agent-implementer", snapshot_text=snapshot,
    ) is False

    assert json.loads(done_path.read_text(encoding="utf-8"))["tests_run"] == newer


def test_an_old_file_without_a_stage_block_is_left_alone(tmp_path):
    """No proven owner — old units keep working exactly as before."""
    project = _project(tmp_path)
    done_path = _outbox(project) / "DONE-TODO-0001.json"
    _write(done_path, {"tests_run": [{"cmd": "true", "result": "ok"}]})

    snapshot = done_owner.snapshot(_outbox(project), "TODO-0001")
    _write(done_path, {"tests_run": [{"cmd": "true", "result": "later"}]})

    assert done_owner.restore(
        _outbox(project), "TODO-0001",
        stage_id="agent-project-auditor", snapshot_text=snapshot,
    ) is False


# ─── The hook is wired, not just the module (A27 stage runner) ─────────


def _stage_project(tmp_path: Path) -> Path:
    project = _project(tmp_path)
    for name in ("inbox", "logs", "roles"):
        (project / ".agentic" / name).mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "roles" / "developer.md").write_text("# dev\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text("# task\n", encoding="utf-8")
    return project


def _run_fake_stage(monkeypatch, project: Path, writes):
    from awf import agent_stage
    from awf.pipeline import Stage

    def fake_run(cmd, cwd, **_kw):
        writes()
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("awf.signal_watch.run_subprocess_until_signal", fake_run)
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "_log", lambda *a, **kw: None)
    stage = Stage(name="qa", role="developer", kind="execute", id="agent-qa-review")
    agent_stage.run_agent_stage(
        stage, "TODO-0001", project,
        {"models": {"developer": {"model": "p/test"}}},
        project / ".agentic" / "logs",
    )


def test_the_stage_runner_puts_the_authors_done_json_back(tmp_path, monkeypatch):
    """The engine hook, not only ``done_owner.restore``: a stage that writes
    DONE as a reader leaves the author's ``tests_run`` in place."""
    project = _stage_project(tmp_path)
    outbox = _outbox(project)
    done_path = _author(project, [{"cmd": "true", "result": "1 passed"}])
    before = done_path.read_text(encoding="utf-8")
    foreign = {
        "tests_run": [{"cmd": "reused canonical suite artifact x", "result": "exit 0"}],
        "stage": {"stage_id": "agent-qa-review", "attempt": 1},
    }

    _run_fake_stage(
        monkeypatch, project,
        lambda: _write(done_path, foreign),
    )

    assert done_path.read_text(encoding="utf-8") == before
    probes = json.loads((outbox / "PROBES-TODO-0001.json").read_text(encoding="utf-8"))
    assert probes["tests_run"] == foreign["tests_run"], "the reader's facts must survive"


def test_the_previous_readers_probes_are_cleared_before_the_spawn(tmp_path, monkeypatch):
    """PROBES is per-stage like PROGRESS: last reader's file cannot stand in
    for this stage's claims (otherwise ``restore`` drops them)."""
    project = _stage_project(tmp_path)
    outbox = _outbox(project)
    _author(project, [{"cmd": "true", "result": "1 passed"}])
    stale = outbox / "PROBES-TODO-0001.json"
    _write(stale, {"tests_run": [{"cmd": "true", "result": "old reader"}]})
    seen: dict = {}
    mine = {"tests_run": [{"cmd": "true", "result": "this reader"}]}

    def writes():
        seen["at_spawn"] = stale.exists()
        _write(stale, mine)

    _run_fake_stage(monkeypatch, project, writes)

    assert seen["at_spawn"] is False, "the previous reader's PROBES reached this stage"
    assert json.loads(stale.read_text(encoding="utf-8"))["tests_run"] == mine["tests_run"]


def test_a_reader_that_deletes_done_json_does_not_erase_the_author(tmp_path):
    """Deleting the author's file is an overwrite with an empty body."""
    project = _project(tmp_path)
    done_path = _author(project, [{"cmd": "true", "result": "1 passed"}])
    before = done_path.read_text(encoding="utf-8")
    snapshot = done_owner.snapshot(_outbox(project), "TODO-0001")

    done_path.unlink()
    restored = done_owner.restore(
        _outbox(project), "TODO-0001",
        stage_id="agent-project-auditor", snapshot_text=snapshot,
    )

    assert restored is True
    assert done_path.read_text(encoding="utf-8") == before
    assert not (_outbox(project) / "PROBES-TODO-0001.json").exists(), (
        "a deleted file has no reader facts to keep")
