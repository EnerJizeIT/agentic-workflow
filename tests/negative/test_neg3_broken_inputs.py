"""NEG-3 (negative-regression layer 3): broken inputs give a clean refusal.

BACKLOG NEG-2026-09, layer 3 (TODO-0114): a corrupt input must not take the
pipeline down with a traceback and must not corrupt state files; the
supervisor gets a typed refusal (AwfApiError / refused / degraded section),
and after the input is repaired the same public path works again.

The AUD12-11 layer (test_broken_inputs.py) pinned state/current.yaml,
run.yaml read via run_status, rollback on a garbage baseline sha, the empty
TODO body via get_status, and a syntactically broken pipeline YAML. This
layer completes the matrix along the run/pipeline public paths:

- (a) corrupt run.yaml (truncated line, YAML garbage, invalid shape) via
  run_status / run_next / run_start;
- (b) semantically corrupt pipeline YAML (stages not a list, garbage root)
  via load_stages / start_pipeline;
- (c) garbage BASELINE-<id>.sha (non-hex, unknown commit, binary) via
  prove_red / verify_pack;
- (d) an empty TODO body via run_next;
- (e) a missing pipeline file (explicit queue item, config default) via
  run_next / start_pipeline;
- (f) a broken DONE-<id>.json (not JSON, binary) via verify_pack /
  collect_done_facts (the worker handoff);
- (g) a binary BASELINE-<id>.include via read_include_list / run_next.

Real holes found by this layer (each test is red on the baseline):
- binary BASELINE-<id>.include → UnicodeDecodeError escaped
  read_include_list and the run_next re-baseline;
- binary DONE-<id>.json → UnicodeDecodeError escaped verify_pack and
  collect_done_facts;
- binary BASELINE-<id>.sha → UnicodeDecodeError escaped verify_pack diff.

The rest pins already-correct degradation so a regression re-introducing a
traceback goes red. No real spawns: run_next launch is stubbed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api._errors import AwfApiError
from awf.api._results import StartResult
from awf.include_untracked import read_include_list
from awf.pipeline import load_stages
from awf.unit_contract import collect_done_facts

VALID_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "implement"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def _init(project: Path) -> Path:
    api.init_project(project, project_name="T")
    return project


def _run_file(project: Path) -> Path:
    return project / ".agentic" / "state" / "run.yaml"


def _set_config_key(project: Path, key: str, value: object) -> None:
    cfg_path = project / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data[key] = value
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _agentic_bytes(project: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted((project / ".agentic").rglob("*")):
        if p.is_file():
            out[str(p.relative_to(project))] = p.read_bytes()
    return out


def _assert_untouched(
    project: Path, before: dict[str, bytes], changed_ok: frozenset[str] = frozenset()
) -> None:
    """Every state file that existed before the call keeps its bytes
    (the corrupt-input call may add files — logs, baselines — but must not
    modify the ones it did not touch)."""
    after = _agentic_bytes(project)
    for rel, blob in before.items():
        if rel in changed_ok:
            continue
        assert after.get(rel) == blob, f"state file {rel} changed by a corrupt-input call"


# ── (a) corrupt run.yaml ─────────────────────────────────────────────────


def test_a_truncated_run_yaml_run_next_refuses_then_recovers(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=["TODO-0001"])
    run_file = _run_file(project)
    valid = run_file.read_text(encoding="utf-8")
    before = _agentic_bytes(project)

    # Obрыв строки: only the first line of the dump survives — parses as a
    # dict without `queue`, i.e. an invalid shape.
    run_file.write_text(valid.splitlines()[0] + "\n", encoding="utf-8")

    result = api.run_next(project)
    assert result.action == "refused"
    assert "No active run" in result.message
    assert api.run_status(project).active is False
    _assert_untouched(project, before, changed_ok={str(run_file.relative_to(project))})

    # Повтор после починки: the original state reads again, the next gate
    # (TODO file) fires instead.
    run_file.write_text(valid, encoding="utf-8")
    result = api.run_next(project)
    assert result.action == "refused"
    assert "TODO-0001.md is missing or empty" in result.message


def test_a_garbage_run_yaml_invalid_shape_degrades(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=["TODO-0001", "TODO-0002"])
    run_file = _run_file(project)
    valid = run_file.read_text(encoding="utf-8")
    before = _agentic_bytes(project)

    # Valid YAML, invalid shape: the queue item is not a TODO-NNNN id.
    run_file.write_text("queue:\n  - not-a-todo-id\nactive: true\n", encoding="utf-8")

    status = api.run_status(project)
    assert status.active is False
    assert status.queue == []
    result = api.run_next(project)
    assert result.action == "refused"
    assert "No active run" in result.message
    _assert_untouched(project, before, changed_ok={str(run_file.relative_to(project))})

    run_file.write_text(valid, encoding="utf-8")
    assert api.run_status(project).active is True


def test_a_yaml_garbage_run_yaml_run_start_replaces_lost_run(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=["TODO-0001"])
    run_file = _run_file(project)
    before = _agentic_bytes(project)

    # YAML-mусор (unparsable): the run is lost, a fresh run_start repairs.
    run_file.write_text("queue: [unclosed\n  - \x00\n: :", encoding="utf-8")

    result = api.run_start(project, queue=["TODO-0003"])
    assert result.active is True
    status = api.run_status(project)
    assert status.active is True
    assert [q["todo_id"] for q in status.queue] == ["TODO-0003"]
    _assert_untouched(project, before, changed_ok={str(run_file.relative_to(project))})


# ── (b) semantically corrupt pipeline YAML ───────────────────────────────


def test_b_stages_not_a_list_load_stages_raises_typed(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(exist_ok=True)
    f = pipes / "default.yaml"
    for body in (
        'name: default\nstages: "not-a-list"\n',
        "name: default\nstages: {plan: supervisor}\n",
        "name: default\nstages: []\n",
    ):
        f.write_text(body, encoding="utf-8")
        with pytest.raises(AwfApiError, match="non-empty list"):
            load_stages(f)


def test_b_garbage_root_load_stages_raises_typed(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(exist_ok=True)
    f = pipes / "default.yaml"
    for body in ("- just\n- a\n- list\n", "just a scalar\n", ""):
        f.write_text(body, encoding="utf-8")
        with pytest.raises(AwfApiError, match="document root must be a mapping"):
            load_stages(f)


def test_b_semantic_pipeline_corruption_start_pipeline_clean_exit(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(exist_ok=True)
    (pipes / "default.yaml").write_text("name: default\nstages: 42\n", encoding="utf-8")

    result = api.start_pipeline(str(project), background=False, auto=True)
    assert result.run_mode == "foreground"
    assert result.exit_code == 1
    assert "non-empty list" in result.message


# ── (c) garbage BASELINE-<id>.sha ────────────────────────────────────────


def test_c_garbage_baseline_sha_prove_red_typed(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    ctx = project / ".agentic" / "context"
    ctx.mkdir(exist_ok=True)
    sha_file = ctx / "BASELINE-TODO-0001.sha"

    sha_file.write_text("zz-not-hex\n", encoding="utf-8")
    with pytest.raises(AwfApiError, match="7-40 char git sha"):
        api.prove_red(project, "TODO-0001", tests=["tests/test_x.py"], tmp_base=tmp_git_repo)

    sha_file.write_text("deadbeef" * 5 + "\n", encoding="utf-8")
    with pytest.raises(AwfApiError, match="not a commit in this repo"):
        api.prove_red(project, "TODO-0001", tests=["tests/test_x.py"], tmp_base=tmp_git_repo)


def test_c_garbage_baseline_sha_verify_pack_degrades(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    ctx = project / ".agentic" / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "BASELINE-TODO-0001.sha").write_text("zz-not-hex\n", encoding="utf-8")

    result = api.verify_pack(project, "TODO-0001", write_report=False)
    assert result.sections["diff"] == "fail"
    assert result.details.get("diff") == "git error"
    assert result.exit_code in (0, 1, 2)


def test_c_binary_baseline_sha_verify_pack_degrades(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    ctx = project / ".agentic" / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "BASELINE-TODO-0001.sha").write_bytes(b"\xff\xfe\x00binary garbage")

    result = api.verify_pack(project, "TODO-0001", write_report=False)
    assert result.sections["diff"] == "skipped"
    assert result.exit_code in (0, 1, 2)


# ── (d) empty TODO body ──────────────────────────────────────────────────


def test_d_empty_todo_body_run_next_refused_then_recovers(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=[{"todo_id": "TODO-0001", "pipeline": "ghost-pipe"}])
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(exist_ok=True)
    todo_md = inbox / "TODO-0001.md"
    todo_md.write_text("")
    before = _agentic_bytes(project)

    result = api.run_next(project)
    assert result.action == "refused"
    assert "TODO-0001.md is missing or empty" in result.message
    assert not (inbox / "TODO-0001.ready").exists()
    _assert_untouched(project, before)

    # Повтор после починки: the body is written, the empty-body gate passes
    # and the next gate (missing pipeline) fires — still no side effects.
    todo_md.write_text("# TODO-0001\nreal task\n", encoding="utf-8")
    result = api.run_next(project)
    assert result.action == "refused"
    assert "ghost-pipe" in result.message
    assert "not found" in result.message
    assert not (inbox / "TODO-0001.ready").exists()


# ── (e) missing pipeline file ────────────────────────────────────────────


def test_e_missing_queue_pipeline_run_next_refused(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=[{"todo_id": "TODO-0001", "pipeline": "ghost-pipe"}])
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# TODO-0001\ntask\n", encoding="utf-8")
    before = _agentic_bytes(project)

    result = api.run_next(project)
    assert result.action == "refused"
    assert "ghost-pipe" in result.message
    assert "not found" in result.message
    # A refused launch leaves no signal behind.
    assert not (inbox / "TODO-0001.ready").exists()
    _assert_untouched(project, before)


def test_e_missing_config_pipeline_start_pipeline_clean_exit(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    # No pipelines/ directory at all: the config-declared name AND the
    # default.yaml fallback are both missing.
    _set_config_key(project, "default_pipeline", "ghost")

    result = api.start_pipeline(str(project), background=False, auto=True)
    assert result.run_mode == "foreground"
    assert result.exit_code == 1


# ── (f) broken DONE-<id>.json ────────────────────────────────────────────


def test_f_broken_done_json_verify_pack_degrades(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    outbox = project / ".agentic" / "outbox"
    outbox.mkdir(exist_ok=True)
    (outbox / "DONE-TODO-0001.json").write_text("{not json at all", encoding="utf-8")

    result = api.verify_pack(project, "TODO-0001", write_report=False)
    assert result.sections["done_json"] == "skipped"
    assert result.exit_code in (0, 1, 2)


def test_f_binary_done_json_verify_pack_degrades(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    outbox = project / ".agentic" / "outbox"
    outbox.mkdir(exist_ok=True)
    (outbox / "DONE-TODO-0001.json").write_bytes(b"\xff\xfe\x00not json")

    result = api.verify_pack(project, "TODO-0001", write_report=False)
    assert result.sections["done_json"] == "skipped"
    assert result.exit_code in (0, 1, 2)


def test_f_broken_done_json_handoff_facts_degrade(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    outbox = project / ".agentic" / "outbox"
    outbox.mkdir(exist_ok=True)
    logs = project / ".agentic" / "logs"
    logs.mkdir(exist_ok=True)
    done_json = outbox / "DONE-TODO-0001.json"

    done_json.write_text("{{{{", encoding="utf-8")
    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)
    assert lines == []
    assert declared is None
    assert "DONE-json=absent" in fact

    done_json.write_bytes(b"\xff\xfe\x00binary")
    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)
    assert lines == []
    assert declared is None
    assert "DONE-json=absent" in fact


# ── (g) binary BASELINE-<id>.include ─────────────────────────────────────


def test_g_binary_include_read_list_degrades_and_recovers(tmp_git_repo: Path):
    project = _init(tmp_git_repo)
    ctx = project / ".agentic" / "context"
    ctx.mkdir(exist_ok=True)
    inc = ctx / "BASELINE-TODO-0001.include"

    inc.write_bytes(b"\xff\xfe\x00binary garbage")
    assert read_include_list(project, "TODO-0001") is None

    # Повтор после починки.
    inc.write_text("docs/notes.md\n", encoding="utf-8")
    assert read_include_list(project, "TODO-0001") == ["docs/notes.md"]

    # The documented empty-file form stays a no-op list.
    inc.write_bytes(b"")
    assert read_include_list(project, "TODO-0001") == []


def test_g_binary_include_run_next_survives(tmp_git_repo: Path, monkeypatch):
    project = _init(tmp_git_repo)
    api.run_start(project, queue=["TODO-0001"])
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# TODO-0001\ntask\n", encoding="utf-8")
    ctx = project / ".agentic" / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / "BASELINE-TODO-0001.include").write_bytes(b"\xff\xfe\x00binary")

    calls: list[str] = []

    def fake_start_pipeline(
        project_dir,
        *,
        background,
        from_stage,
        auto,
        timeout,
        pipeline,
        todo_id,
        no_checkpoints,
    ) -> StartResult:
        calls.append(todo_id)
        return StartResult(
            run_mode="background",
            run_id=4242,
            log_file=None,
            exit_code=None,
            message="stub",
        )

    monkeypatch.setattr("awf.api.pipeline.start_pipeline", fake_start_pipeline)

    result = api.run_next(project)
    assert result.action == "started"
    assert result.todo_id == "TODO-0001"
    assert calls == ["TODO-0001"]
