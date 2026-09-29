"""ORCH M7.3 (ORCH-05 приёмка): аналитический сквозной сценарий.

Стратегия §7: постоянный supervisor + сменяемые pipeline на разных
составах. M4.4 довел до сквозного кодовый и документный составы;
аналитический (не-код: сбор требований → обзор → итоговый документ)
оставался без сквозняка. Этот юнит закрывает его:

1. Состав ``analyst → reviewer → writer`` с объявленными выходами
   (аналитические документы) и profile M7.2 на аналитике (``tools:
   deny: [edit, write]`` — аналитик без edit): полный прогон до коммита.
   Выходы создаются и проверяются движком (свежесть, M3.2), стадии
   адресуются, handoff'ы по stage_id + Stage facts (M4.3), снимок
   pipeline несёт профиль (M3.3 + M7.2).
2. Факт по судьбе документов в коммите: что попадает в unit-коммит
   не-кодовой задачи (приёмка для не-код задач).
3. Снимок уважается: переопубликованный pipelines/*.yaml не меняет
   запущенный юнит — resume идёт по PIPELINE-{todo}.yaml.

Шим, tmp-проект, без сети и живых процессов (тот же приём, что в
test_orch_composition_scenarios.py).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml
from conftest import _git_init, run_awf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

CONFIG_YAML = (
    "project:\n"
    "  name: m73-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

ROLE_FILES = {
    "supervisor": "# Supervisor\nPlan and verify.\n",
    "analyst": "# Analyst\nGather and structure requirements.\n",
    "reviewer": "# Reviewer\nReview the requirements.\n",
    "writer": "# Writer\nWrite the final analysis.\n",
}

# Аналитический состав: не-код, выходы — документы. Аналитик (M7.2) без
# edit/write: профиль сузает его tools, declared output остаётся контрактом
# стадии (движок проверяет файл, а не то, каким tool он создан).
ANALYTICAL_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "analyst"\n'
    '    role: "analyst"\n'
    '    task: "Gather and structure the requirements"\n'
    '    output: "docs/requirements.md"\n'
    "    tools:\n"
    "      deny: [edit, write]\n"
    '    on_blocked: "stop"\n'
    '  - name: "reviewer"\n'
    '    role: "reviewer"\n'
    '    task: "Review the requirements for gaps"\n'
    '    input: "docs/requirements.md"\n'
    '    output: "docs/review-notes.md"\n'
    '  - name: "writer"\n'
    '    role: "writer"\n'
    '    task: "Write the final analysis"\n'
    '    input: "docs/review-notes.md"\n'
    '    output: "docs/analysis.md"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "commit_and_next"\n'
)

# Переопубликованный default.yaml для сценария снимка: валидный, но чужой
# состав. Если resume пойдёт по нему, analyst не найдётся (отдельная стадия
# исчезла) — прогон упадёт с "Stage 'analyst' not found" и без документов.
REPLACED_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "commit_and_next"\n'
)

DOCS = ("docs/requirements.md", "docs/review-notes.md", "docs/analysis.md")
STAGE_OF_DOC = {
    "docs/requirements.md": "analyst",
    "docs/review-notes.md": "reviewer",
    "docs/analysis.md": "writer",
}


def _skeleton(proj: Path) -> None:
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff", "state"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    for role, content in ROLE_FILES.items():
        (ag / "roles" / f"{role}.md").write_text(content, encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")


def _make_project(tmp_path: Path, pipeline_yaml: str, todo_id: str = "TODO-0001") -> Path:
    """Throwaway git project: .agentic skeleton + one pipeline + one TODO."""
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    _skeleton(proj)
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(pipeline_yaml, encoding="utf-8")
    inbox = proj / ".agentic" / "inbox"
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub analytical task\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    # BD-8: pre-authorize the verify commit so --auto does not wait in the gate.
    (inbox / f"APPROVE-{todo_id}.ready").write_text(
        f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
    )
    return proj


def _env(awf_env: dict, state_dir: Path, stage_modes: str = "") -> dict:
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = "do-step"
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    # The stages declare document outputs; let the shim create them so the
    # engine's exists+fresh check (M3.2) passes.
    env["AWF_SHIM_CREATE_DECLARED_OUTPUT"] = "1"
    if stage_modes:
        env["AWF_SHIM_STAGE_MODES"] = stage_modes
    state_dir.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _out(run) -> str:
    return (run.stdout + run.stderr).decode(errors="replace")


def _git(proj: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


def _handoff(proj: Path, stage: str, todo_id: str = "TODO-0001") -> str:
    p = proj / ".agentic" / "done" / todo_id / "handoff" / f"{stage}-{todo_id}.md"
    assert p.is_file(), f"handoff {p.name} missing"
    return p.read_text(encoding="utf-8")


def _snapshot_stages(proj: Path, todo_id: str = "TODO-0001") -> list[dict]:
    """The per-TODO pipeline snapshot (M3.3) as stage dicts (same loader
    document form; the header comments are dropped before parsing)."""
    snap = proj / ".agentic" / "context" / f"PIPELINE-{todo_id}.yaml"
    assert snap.is_file(), f"pipeline snapshot missing: {snap}"
    text = "\n".join(
        line for line in snap.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    )
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict) and isinstance(doc.get("stages"), list), text
    return doc["stages"]


def _assert_docs_created(proj: Path, todo_id: str = "TODO-0001") -> None:
    """The declared outputs exist and were written by their own stage."""
    for doc in DOCS:
        p = proj / doc
        assert p.is_file(), f"declared output {doc} missing"
        expected = f'shim output for stage "{STAGE_OF_DOC[doc]}" (TODO {todo_id})'
        assert expected in p.read_text(encoding="utf-8"), (doc, p.read_text())


# ── сценарий 1: аналитический состав, полный прогон до коммита ───────────


@pytest.mark.timeout(600)
def test_analytical_composition_full_run(tmp_path, awf_bin, awf_env):
    """analyst (M7.2 profile, без edit/write) → reviewer → writer, выходы —
    документы. Прогон до коммита: выходы создаются и проверяются движком,
    стадии адресуются, handoff'ы с Stage facts, снимок несёт профиль, unit-
    коммит содержит declared output-документы."""
    proj = _make_project(tmp_path, ANALYTICAL_PIPELINE)
    state1 = tmp_path / "state1"
    env = _env(awf_env, state1)

    started = time.monotonic()
    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=300,
    )
    elapsed = time.monotonic() - started
    out1 = _out(run1)
    assert run1.returncode == 0, f"analytical run must complete. rc={run1.returncode}\n{out1}"
    assert "Pipeline complete!" in out1, out1
    # Runtime within the e2e limits (measured, invariant 5).
    assert elapsed < 240, f"run took {elapsed:.0f}s — outside the e2e limits"

    # Stages are addressed by their names in order.
    assert "Stages: plan analyst reviewer writer verify" in out1, out1

    # Declared document outputs were created by their stages and the engine
    # advanced past each exists+fresh check (M3.2); a failed check would
    # route the stage as blocked and stop the run.
    _assert_docs_created(proj)
    assert "did not fulfill its assignment" not in out1, out1

    # The analyst's assignment (task + expected output) reached the stage
    # prompt — that is what the shim (and a real agent) acts on.
    analyst_prompt = _find_prompt(state1, "Gather and structure the requirements")
    assert "Expected output: docs/requirements.md" in analyst_prompt, analyst_prompt[:800]

    # Handoffs are per stage (file per stage, addressed by its name) with the
    # engine-filled Stage facts (M4.3) — the chain a next worker reads.
    for stage in ("analyst", "reviewer", "writer"):
        h = _handoff(proj, stage)
        assert f"# Handoff from `{stage}` (TODO TODO-0001)" in h, h
        assert "## Stage facts" in h, f"{stage}: Stage facts section missing\n{h}"
        assert f"- stage_id: `{stage}`" in h, f"{stage}: stage_id missing\n{h}"
        assert f"- role: `{stage}`" in h, f"{stage}: role missing\n{h}"
        assert "- attempt: 1" in h, f"{stage}: attempt missing\n{h}"

    # The pipeline snapshot (M3.3) captured the composition WITH the M7.2
    # profile — the resume source keeps the stage's tools contract.
    snap_stages = _snapshot_stages(proj)
    assert [s["name"] for s in snap_stages] == [
        "plan", "analyst", "reviewer", "writer", "verify",
    ], snap_stages
    analyst = next(s for s in snap_stages if s["name"] == "analyst")
    assert analyst.get("tools", {}).get("deny") == ["edit", "write"], analyst

    # Invariant 3: the unit commit contains the declared output documents.
    _assert_unit_commit_contains_docs(proj)

    # The run archived the TODO (verify approve -> commit_and_next -> archive).
    assert (proj / ".agentic" / "done" / "TODO-0001" / "DONE.md").is_file(), \
        "TODO-0001 not archived to done/"
    print(f"\nanalytical full run: {elapsed:.1f}s")


def _find_prompt(state_dir: Path, marker: str) -> str:
    for p in sorted(state_dir.glob("prompt-*-worker.txt")):
        text = p.read_text(encoding="utf-8")
        if marker in text:
            return text
    raise AssertionError(f"no worker prompt contains {marker!r} in {state_dir}")


def _assert_unit_commit_contains_docs(proj: Path, todo_id: str = "TODO-0001") -> None:
    """The verify commit (on_approved: commit_and_next) committed the unit's
    work. Fact under check (invariant 3): the declared output documents are
    IN the commit — a non-code unit's product is its documents."""
    log = _git(proj, "log", "--oneline")
    lines = [line for line in log.splitlines() if line.strip()]
    # init + ignore .agentic + the unit commit.
    assert len(lines) == 3, f"expected 3 commits (init, gitignore, unit), got: {log}"
    top = _git(proj, "log", "-1", "--format=%s")
    assert top == f"awf(verify): {todo_id}", f"unexpected top commit subject: {top}"
    files = _git(proj, "show", "--name-only", "--format=", "HEAD")
    for doc in DOCS:
        assert doc in files, f"declared output {doc} NOT in the unit commit:\n{files}"


# ── сценарий 2: переопубликованный default.yaml не меняет запущенный юнит ─


@pytest.mark.timeout(600)
def test_republished_pipeline_does_not_change_running_unit(tmp_path, awf_bin, awf_env):
    """Снимок уважается (M3.3). Запуск на аналитическом составе, стоп на
    analyst (BLOCKED, on_blocked: stop). Между запусками pipelines/default.
    yaml переопубликован на чужой состав (без analyst). Ответ супервизора
    `continue --ack` резюмирует юнит по снимку PIPELINE-TODO-0001.yaml
    (юнит закреплён ack'ом, стадия — из state): стадии оригинального
    состава, документы создаются, прогон завершается. По переопубликованному
    файлу analyst не нашёлся бы — "Stage 'analyst' not found" и rc=1."""
    proj = _make_project(tmp_path, ANALYTICAL_PIPELINE)

    # Phase 1: the run starts, the snapshot is captured at launch, the
    # analyst stage blocks and the run stops by policy.
    env1 = _env(awf_env, tmp_path / "state1", stage_modes="analyst=write-blocked")
    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env1, input_data=b"", timeout=300,
    )
    out1 = _out(run1)
    assert run1.returncode != 0, f"run1 must stop on the blocked analyst. rc={run1.returncode}\n{out1}"
    assert "Pipeline stopped by policy." in out1, out1

    snap = proj / ".agentic" / "context" / "PIPELINE-TODO-0001.yaml"
    assert snap.is_file(), "pipeline snapshot missing after the first launch"
    snap_before = snap.read_text(encoding="utf-8")

    # Between launches: republish the pipeline file with a foreign composition.
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        REPLACED_PIPELINE, encoding="utf-8"
    )

    # Phase 2: the supervisor's answer to the blocked unit — `continue --ack`
    # writes ACK, pins the unit, and resumes from the state's stage (analyst).
    env2 = _env(awf_env, tmp_path / "state2")
    run2 = run_awf(
        awf_bin, ["continue", "--auto", "--ack", "TODO-0001"],
        cwd=proj, env=env2, input_data=b"", timeout=300,
    )
    out2 = _out(run2)
    assert run2.returncode == 0, (
        f"resume must run the snapshot composition. rc={run2.returncode}\n{out2}"
    )
    assert "not found in pipeline" not in out2, out2
    assert "Pipeline complete!" in out2, out2
    assert "Stages: plan analyst reviewer writer verify" in out2, out2

    # The unit produced its documents on the snapshot composition.
    _assert_docs_created(proj)

    # The snapshot is untouched by the republish — it still carries the
    # original composition and the analyst's tools profile.
    snap_after = snap.read_text(encoding="utf-8")
    assert snap_after == snap_before, "republishing default.yaml rewrote the snapshot"
    snap_stages = _snapshot_stages(proj)
    assert [s["name"] for s in snap_stages] == [
        "plan", "analyst", "reviewer", "writer", "verify",
    ], snap_stages
    analyst = next(s for s in snap_stages if s["name"] == "analyst")
    assert analyst.get("tools", {}).get("deny") == ["edit", "write"], analyst
