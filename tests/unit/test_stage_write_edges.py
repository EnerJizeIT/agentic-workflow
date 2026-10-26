"""TODO-0257 — K5 edges: commit refusal, record naming, non-UTF-8, ``./``.

The fence landed in TODO-0254/0256; this file pins the edges the reject of
0256 named (P2–P5). Each test reproduces the reported defect: the refusal
must not crash on a relative root or a mismatched record name, a non-UTF-8
file name must reach BLOCKED (not ``UnicodeEncodeError``), a ``./``-prefixed
``output`` must not become a false BLOCKED, and a declared ``input`` under
``.agentic/`` is read-only and loads again.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from awf import pipeline_engine, stage_manifest
from awf.pipeline import Stage, validate_pipeline_stages
from awf.pipeline_engine import _stage_write_violation
from tests.spec_support import init_project_repo

TODO = "TODO-0001"


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _patch_engine(monkeypatch, worker, state_calls: list[dict]):
    escalations: list[str] = []

    def fake_escalate(project_dir, logs_dir, s_name, todo, *a, **kw):
        escalations.append(s_name)
        return (1, todo, 0)

    def fake_state(project_dir, **kwargs):
        state_calls.append(kwargs)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_write_state", fake_state)
    monkeypatch.setattr(pipeline_engine, "_handle_escalate", fake_escalate)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    return escalations


def _run(repo: Path, stage: Stage, todo: str = TODO) -> tuple[str, int, int]:
    return pipeline_engine.execute_agent_stage(
        stage=stage,
        current_todo=todo,
        project_dir=repo,
        config={},
        logs_dir=repo / ".agentic" / "logs",
        stages=[stage],
        stage_idx=0,
        retry_counts=[0],
        auto=False,
        agent_hard_timeout=None,
    )


# --- P2: the refusal names the real record, survives a relative root ----


def test_violation_refusal_accepts_a_relative_project_dir(tmp_path, monkeypatch):
    """P2a: ``violation_refusal('.')`` must refuse, not raise ``ValueError``."""
    repo = init_project_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "evil.py").write_text("secret = 1\n", encoding="utf-8")
    stage_manifest.record_violation(
        repo,
        TODO,
        "worker",
        allowlist=("docs/out.md",),
        paths_outside=["src/evil.py"],
    )

    monkeypatch.chdir(repo)
    message = stage_manifest.violation_refusal(".")
    assert "open write violation" in message
    assert "src/evil.py" in message
    assert f"WRITE-VIOLATION-{TODO}.json" in message


def test_violation_refusal_names_the_actual_record_file(tmp_path):
    """P2b: the remedy points at the file stem, not the ``todo_id`` field."""
    repo = init_project_repo(tmp_path)
    context = repo / ".agentic" / "context"
    context.mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir()
    target = repo / "docs" / "out.md"
    target.write_text("rejected\n", encoding="utf-8")
    record = context / "WRITE-VIOLATION-TODO-0100.json"
    record.write_text(
        json.dumps(
            {
                "todo_id": "TODO-0200",
                "stage": "worker",
                "paths": {
                    "docs/out.md": {
                        "before": "<missing>",
                        "after": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                },
                "error": "",
            }
        ),
        encoding="utf-8",
    )

    message = stage_manifest.violation_refusal(repo)
    assert "WRITE-VIOLATION-TODO-0100.json" in message
    assert "WRITE-VIOLATION-TODO-0200.json" not in message


# --- P3: a non-UTF-8 name blocks, the fail-closed writers do not crash --


def test_non_utf8_path_is_blocked_not_crashing(tmp_path, monkeypatch):
    """P3: a non-UTF-8 name reaches BLOCKED; the fail-closed branch is safe."""
    from awf import commit_plan

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    bad_name = "bad\udcffname.md"

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / bad_name).write_bytes(b"secret\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, fake_worker, state_calls)
    _run(repo, stage)

    assert escalations == ["worker"]
    record = repo / ".agentic" / "context" / f"WRITE-VIOLATION-{TODO}.json"
    assert record.is_file()
    data = json.loads(record.read_text(encoding="utf-8"))
    assert any("bad" in path for path in data["paths"])
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{TODO}.md"
    assert note.is_file()
    note.read_text(encoding="utf-8")  # the note is valid UTF-8 text
    message = stage_manifest.violation_refusal(repo)
    assert "bad" in message
    assert commit_plan.build_commit_plan(repo, TODO).refusal


def test_non_utf8_todo_id_reaches_blocked_not_a_crash(tmp_path, monkeypatch):
    """P3 (second surface): the fail-closed writers escape the TODO id too.

    The record name, the STAGE-DIFF header, the BLOCKED note and the refusal
    message are built from ``todo_id``, and ``todo_id`` becomes a path
    component before any validation (``awf start`` picks
    ``todos.newest_active`` — an inbox file name is not checked).  A name
    decoded with ``surrogateescape`` therefore reached the fail-closed
    writers RAW: the STAGE-DIFF write raised ``UnicodeEncodeError`` from
    inside the stage, so the violation was never recorded and the stage
    never routed as BLOCKED.  Same rule as the touched paths — escape for
    the report, keep the raw bytes for the path.
    """
    from awf import commit_plan

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    todo = "TODO-01\udcff"
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, fake_worker, state_calls)
    _run(repo, stage, todo)

    assert escalations == ["worker"], "a violation must route as blocked"
    diff_report = repo / ".agentic" / "context" / f"STAGE-DIFF-{todo}-worker.md"
    assert diff_report.is_file()
    diff_report.read_text(encoding="utf-8")  # strict UTF-8: no raw surrogates
    record = repo / ".agentic" / "context" / f"WRITE-VIOLATION-{todo}.json"
    assert record.is_file(), "the commit gate reads this record"
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{todo}.md"
    assert note.is_file()
    note.read_text(encoding="utf-8")
    refusal = commit_plan.build_commit_plan(repo, todo).refusal
    assert refusal
    refusal.encode("utf-8")  # the log/refusal writer must not crash


def test_start_refuses_a_non_utf8_todo_id_before_any_stage(tmp_path):
    """P3 (launch surface): a malformed unit id is refused, not launched."""
    from awf.api.pipeline import start_pipeline

    repo = init_project_repo(tmp_path)
    result = start_pipeline(repo, background=True, todo_id="TODO-01\udcff")

    assert result.run_mode == "noop"
    assert result.exit_code == 1
    assert "Invalid TODO id" in result.message
    assert "TODO-NNNN" in result.message


def test_continue_refuses_a_non_utf8_todo_id_before_any_stage(tmp_path):
    """P3 (resume surface): the same refusal, one text and shape."""
    from awf.api.pipeline import continue_pipeline

    repo = init_project_repo(tmp_path)
    result = continue_pipeline(repo, todo_id="TODO-01\udcff", background=True)

    assert result.run_mode == "noop"
    assert result.exit_code == 1
    assert "Invalid TODO id" in result.message


# --- P4: ``./`` in a declared output is the same path as git's ----------


def test_dot_slash_output_is_not_a_false_violation(tmp_path):
    """P4: ``output: ./docs/out.md`` matches git's ``docs/out.md``."""
    repo = init_project_repo(tmp_path)
    stage = Stage(name="worker", role="worker", kind="execute", output="./docs/out.md")
    assert stage_manifest.is_allowed("docs/out.md", ("./docs/out.md",))
    assert stage_manifest.is_allowed("./docs/out.md", ("docs/out.md",))

    before = stage_manifest.snapshot(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "out.md").write_text("ok\n", encoding="utf-8")
    assert _stage_write_violation(repo, TODO, "worker", before, stage) == []
    assert not (
        repo / ".agentic" / "context" / f"STAGE-DIFF-{TODO}-worker.md"
    ).exists()


# --- P5: the reserved-surface ban is a write rule -----------------------


def test_input_under_agentic_is_readable_again():
    """P5: ``input`` is read-only, so ``.agentic/`` is not a write surface."""
    stages = validate_pipeline_stages(
        [
            {
                "role": "worker",
                "input": ".agentic/context/REPORT.md",
                "output": "docs/out.md",
            }
        ],
        "t.yaml",
    )
    assert stages[0]["input"] == ".agentic/context/REPORT.md"


# --- QA TODO-0257: the fail-closed writers keep their promises ----------


def test_stage_diff_report_failure_keeps_the_violation(
    tmp_path, monkeypatch
):
    """F1: a failed STAGE-DIFF write must not drop the violation.

    The report is the human trace; the caller's record is the guarantee the
    commit gate reads. Crashing on the report write (``ManagedPathError``)
    used to skip ``record_violation`` and free the rejected path.
    """
    from awf.infra import managed_fs

    repo = init_project_repo(tmp_path)
    before = stage_manifest.snapshot(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "out.md").write_text("ok\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "evil.py").write_text("secret\n", encoding="utf-8")
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    real = managed_fs.managed_write_text

    def fail_the_report(target, text, **kwargs):
        if "STAGE-DIFF" in str(target):
            raise managed_fs.ManagedPathError("report write refused")
        return real(target, text, **kwargs)

    monkeypatch.setattr(managed_fs, "managed_write_text", fail_the_report)

    bad = _stage_write_violation(repo, TODO, "worker", before, stage)

    assert bad == ["src/evil.py"], "the violation must be returned, not swallowed"
    assert not (
        repo / ".agentic" / "context" / f"STAGE-DIFF-{TODO}-worker.md"
    ).exists(), "the report write really failed"


def test_engine_blocked_note_feeds_blocked_draft(tmp_path, monkeypatch):
    """F2: the engine's K5 BLOCKED note parses as the standard schema.

    ``awf blocked-draft`` reads ``## НАХОДКИ`` / ``## НАПРАВЛЕНИЯ-ФИКСА``;
    the engine note used a free-form layout it could not read, so the cause
    never reached the supervisor's replan tool.
    """
    from awf import api

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret\n", encoding="utf-8")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    _patch_engine(monkeypatch, fake_worker, state_calls)
    _run(repo, stage)

    draft = api.blocked_draft(repo, TODO)
    assert "src/evil.py" in draft.review_text
    assert f"WRITE-VIOLATION-{TODO}.json" in draft.review_text
    assert "Скоуп:" in draft.review_text


def test_blocked_writer_logs_a_surrogate_error_without_crashing(
    tmp_path, monkeypatch
):
    """P3: the fail-closed branch logs its own failure — a surrogate in the
    error text must not turn the BLOCKED route into an uncaught
    ``UnicodeEncodeError`` (the logger writes strict UTF-8).
    """
    from awf.infra import managed_fs

    repo = init_project_repo(tmp_path)
    todo = "TODO-01\udcff"
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def refuse_to_record(*args, **kwargs):
        raise managed_fs.ManagedPathError(f"managed path refused: {todo}")

    monkeypatch.setattr(stage_manifest, "record_violation", refuse_to_record)

    recorded = pipeline_engine._block_on_write_violation(
        repo,
        todo,
        "worker",
        stage,
        ["src/evil.py"],
        "",
        repo / ".agentic" / "logs",
        None,
    )

    assert recorded is False, "an unwritten record must stop the stage, not crash"
