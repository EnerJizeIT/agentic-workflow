"""ORCH M3.3 (контракт забега и поручения): снимок pipeline на TODO.

Дефект (воспроизведён): каждый запуск резолвит стадии из опубликованного
pipelines/*.yaml заново — правка шаблона между start и continue меняет
возобновляемый юнит посреди цикла: стадии, их имена и политики подменяются,
а контракт юнита (TODO с verify/gates) остаётся прежним.

Инварианты (TODO-0137):
1. При запуске юнита (все пути: awf_start / awf_run_next / awf_continue)
   резолвнутые стадии фиксируются снимком
   .agentic/context/PIPELINE-{todo_id}.yaml — атомарно, с именем исходника
   и временем фиксации.
2. awf continue возобновляет по СНИМКУ: правка опубликованного pipeline
   между запуском и continue не меняет возобновляемый юнит.
3. Снимок отсутствует (старый юнит / ручной запуск) → fallback на живой
   pipeline + предупреждение; деградация, не отказ.
4. Снимок не ломает e2e/salvage; имя файла не конфликтует с
   context-артефактами (BASELINE/DONE/RUN-EVIDENCE/VERIFIED/GATES/CHECKPOINT).
5. Формат снимка — тот же формат стадий, что читает движок (load_stages),
   без второго парсера.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import _git_init, run_awf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

CONFIG_YAML = (
    "project:\n"
    "  name: m33-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

# The unit's own definition: plan -> implement (worker) -> verify.
PIPELINE_A = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)

# The republished template between launch and continue: the worker stage is
# renamed to "intruder" and runs a different role (auditor). If continue
# resumed from this file, the unit would run a stage it never had.
PIPELINE_B = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "intruder"\n'
    '    role: "auditor"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)

# Live pipeline after the snapshot vanished: A plus a new "extra" worker
# stage between implement and verify. A fallback run to the LIVE pipeline
# executes two worker stages; the vanished snapshot (A) had one.
PIPELINE_C = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - id: "extra"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def _make_project(tmp_path: Path, pipeline_yaml: str, with_todo: bool = True) -> Path:
    """Throwaway git project: .agentic skeleton + pipeline + (optional) TODO."""
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    (ag / "pipelines" / "default.yaml").write_text(pipeline_yaml, encoding="utf-8")
    (ag / "roles" / "worker.md").write_text("# Worker\nExecute the TODO.\n", encoding="utf-8")
    (ag / "roles" / "supervisor.md").write_text("# Supervisor\nPlan and verify.\n", encoding="utf-8")
    (ag / "roles" / "auditor.md").write_text("# Auditor\nAudit.\n", encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    if with_todo:
        inbox = ag / "inbox"
        (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub task\n", encoding="utf-8")
        (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
    return proj


def _env(awf_env: dict, worker_mode: str, state_dir: Path) -> dict:
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = worker_mode
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _invocations(state_dir: Path) -> list[list[str]]:
    log = state_dir / "invocation.log"
    if not log.is_file():
        return []
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(proj: Path) -> dict:
    import yaml

    p = proj / ".agentic" / "state" / "current.yaml"
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _snapshot(proj: Path) -> Path:
    return proj / ".agentic" / "context" / "PIPELINE-TODO-0001.yaml"


# ── prove_red 1: continue возобновляет юнит по снимку ─────────────────────


@pytest.mark.timeout(300)
def test_continue_resumes_from_snapshot(tmp_path, awf_bin, awf_env):
    """ORCH M3.3.1+2: запуск фиксирует снимок PIPELINE-{todo}.yaml; правка
    опубликованного pipeline между start и continue не меняет юнит —
    continue исполняет стадии из снимка."""
    proj = _make_project(tmp_path, PIPELINE_A)
    snap = _snapshot(proj)

    # Launch (pinned), worker crashes at implement: the run dies mid-cycle
    # and leaves state (stage_name=implement, todo_id) for the continue.
    state1 = tmp_path / "state1"
    env1 = _env(awf_env, "crash", state1)
    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env1, input_data=b"", timeout=180,
    )
    out1 = (run1.stdout + run1.stderr).decode(errors="replace")
    assert run1.returncode != 0, f"crashed worker must stop the run. rc={run1.returncode}\n{out1}"
    assert "implement" in _state(proj).get("stage_name", ""), _state(proj)

    # Invariant 1: the launch captured the unit's resolved stages.
    assert snap.is_file(), (
        f"expected {snap} after the launch (invariant 1: capture at start). "
        f"context={sorted(p.name for p in (proj / '.agentic/context').glob('*'))}\n{out1}"
    )
    snap_text = snap.read_text(encoding="utf-8")
    assert "implement" in snap_text and "worker" in snap_text, snap_text
    # baseline naming + capture time in the header
    assert re.search(r"captured: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", snap_text), snap_text

    # Republish the template: the worker stage becomes a foreign "intruder".
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(PIPELINE_B, encoding="utf-8")

    # Continue (foreground, auto): must resume the unit on the SNAPSHOT
    # definition.
    state2 = tmp_path / "state2"
    env2 = _env(awf_env, "do-step", state2)
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=env2, input_data=b"", timeout=180,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, f"continue must complete the unit. rc={run2.returncode}\n{out2}"
    assert "Pipeline complete!" in out2, out2

    # The run announced the snapshot's stages, not the republished ones.
    assert "Stages: plan implement verify" in out2, out2
    assert "intruder" not in out2, out2
    # The worker invocation carried the snapshot stage's role, not auditor.
    kinds = [c for c in _invocations(state2) if c[1] == "worker"]
    assert kinds, f"no worker invocation in {_invocations(state2)}"
    assert all(c[3] == "awf-worker-TODO-0001" for c in kinds), kinds
    # The snapshot itself was not overwritten by the republished template.
    assert "implement" in snap.read_text(encoding="utf-8"), "snapshot rewritten by continue"
    assert "intruder" not in snap.read_text(encoding="utf-8"), "snapshot rewritten by continue"


# ── инвариант 3: снимок отсутствует — деградация, не отказ ───────────────


@pytest.mark.timeout(300)
def test_missing_snapshot_falls_back_to_live(tmp_path, awf_bin, awf_env):
    """ORCH M3.3.3: continue старшего юнита без снимка (или после его утраты)
    не отказывается: живой pipeline + предупреждение, и снимок доуказывается
    из живой версии, чтобы следующий continue был стабилен."""
    proj = _make_project(tmp_path, PIPELINE_A)
    snap = _snapshot(proj)
    state1 = tmp_path / "state1"
    env1 = _env(awf_env, "crash", state1)
    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env1, input_data=b"", timeout=180,
    )
    assert run1.returncode != 0, "crashed worker must stop the run"
    assert snap.is_file(), "launch must capture the snapshot"

    # Simulate the old-unit / lost-snapshot case and a template change.
    snap.unlink()
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(PIPELINE_C, encoding="utf-8")

    state2 = tmp_path / "state2"
    env2 = _env(awf_env, "do-step", state2)
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=env2, input_data=b"", timeout=180,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, f"fallback must not refuse the resume. rc={run2.returncode}\n{out2}"
    assert "Pipeline complete!" in out2, out2
    # The warning names the missing snapshot (degradation, not silence).
    assert re.search(
        r"no pipeline snapshot for TODO-0001", out2, re.IGNORECASE,
    ), out2
    # The LIVE pipeline ran (two worker stages), not the vanished snapshot.
    workers = [c for c in _invocations(state2) if c[1] == "worker"]
    assert len(workers) == 2, f"expected the live pipeline's two worker stages, got {workers}"
    # The snapshot was re-captured from the live definition.
    assert snap.is_file(), "fallback must re-capture the snapshot"
    assert "extra" in snap.read_text(encoding="utf-8"), snap.read_text(encoding="utf-8")


# ── инвариант 1, все пути: unpinned start фиксирует снимок после plan ────


@pytest.mark.timeout(300)
def test_unpinned_start_captures_snapshot_after_plan(tmp_path, awf_bin, awf_env):
    """ORCH M3.3.1: запуск без пина (awf_start без todo_id) — TODO узнаётся
    в plan-стадии; снимок фиксируется, как только юнит определён."""
    # No TODO in the inbox: the plan shim (plan-todo mode) creates it.
    proj = _make_project(tmp_path, PIPELINE_A, with_todo=False)
    env = dict(_env(awf_env, "crash", tmp_path / "state1"))
    env["AWF_SHIM_SUPERVISOR_MODE"] = "plan-todo"

    run1 = run_awf(
        awf_bin, ["start", "--auto"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    out1 = (run1.stdout + run1.stderr).decode(errors="replace")
    assert run1.returncode != 0, f"crashed worker must stop the run. rc={run1.returncode}\n{out1}"

    assert _state(proj).get("todo_id") == "TODO-0001", _state(proj)
    assert _state(proj).get("stage_name") == "implement", _state(proj)
    snap = _snapshot(proj)
    assert snap.is_file(), (
        f"expected {snap} after the unpinned launch (invariant 1, all paths). "
        f"context={sorted(p.name for p in (proj / '.agentic/context').glob('*'))}\n{out1}"
    )
    snap_text = snap.read_text(encoding="utf-8")
    assert "implement" in snap_text and "worker" in snap_text, snap_text


# ── инвариант 5: формат снимка = формат стадий движка ────────────────────


def test_snapshot_roundtrip_through_load_stages(tmp_path):
    """ORCH M3.3.5: снимок читается тем же load_stages — id/task/input/
    output/пolicies не теряются на круге Stage -> YAML -> Stage."""
    from awf.pipeline import load_stages, pipeline_snapshot_text

    src = tmp_path / "src.yaml"
    src.write_text(
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - id: draft\n    role: writer\n"
        '    task: "draft it"\n    input: "outline.md"\n    output: "draft.md"\n'
        '    on_blocked: "stop"\n    max_retries: 2\n    max_rollbacks: 1\n'
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    stages = load_stages(src)
    text = pipeline_snapshot_text(stages, "default", "TODO-0137")

    snap = tmp_path / "PIPELINE-TODO-0137.yaml"
    snap.write_text(text, encoding="utf-8")
    back = load_stages(snap)
    assert [s.name for s in back] == [s.name for s in stages]
    assert back[1].id == "draft"
    assert back[1].task == "draft it"
    assert back[1].input == "outline.md"
    assert back[1].output == "draft.md"
    assert back[1].on_blocked == "stop"
    assert back[1].max_retries == 2
    assert back[1].max_rollbacks == 1
    assert back[1].kind == "execute"
    assert back[0].kind == "plan"
    assert back[2].kind == "verify"


# ── инвариант 4: имя снимка не конфликтует с context-артефактами ─────────


def test_snapshot_name_does_not_clobber_context_artifacts(tmp_path):
    """ORCH M3.3.4: запись снимка не трогает существующие артефакты context
    (BASELINE/DONE/RUN-EVIDENCE/VERIFIED/GATES/CHECKPOINT) и оставляет ровно
    один PIPELINE-файл на юнит."""
    from awf.orchestrator import _write_pipeline_snapshot
    from awf.pipeline import Stage

    proj = tmp_path / "proj"
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True)
    artifacts = {
        "BASELINE-TODO-0001.sha": "abc123\n",
        "DONE-TODO-0001.md": "# done\n",
        "RUN-EVIDENCE-TODO-0001.md": "# evidence\n",
        "VERIFIED-TODO-0001.sha": "def456\n",
        "GATES-TODO-0001.md": "# gates\n",
        "CHECKPOINT-TODO-0001.json": "{}\n",
    }
    for name, content in artifacts.items():
        (ctx / name).write_text(content, encoding="utf-8")

    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True)
    stages = [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="implement", role="worker", kind="execute"),
        Stage(name="verify", role="supervisor", kind="verify"),
    ]
    _write_pipeline_snapshot(
        proj, stages, proj / ".agentic" / "pipelines" / "default.yaml",
        "TODO-0001", logs,
    )

    for name, content in artifacts.items():
        assert (ctx / name).read_text(encoding="utf-8") == content, f"{name} clobbered"
    pipeline_files = sorted(p.name for p in ctx.glob("PIPELINE-*"))
    assert pipeline_files == ["PIPELINE-TODO-0001.yaml"], pipeline_files


# ── QA review (M3.3): corrupted snapshot — degrade, not refuse ───────────


@pytest.mark.timeout(300)
def test_byte_corrupted_snapshot_degrades_and_recaptures(tmp_path, awf_bin, awf_env):
    """QA review (M3.3): a snapshot corrupted at the byte level (not even
    valid YAML) degrades the same way as a missing one — warning, the live
    pipeline, and a re-captured valid snapshot. The unit must not die on
    its own shadow file (documented contract)."""
    proj = _make_project(tmp_path, PIPELINE_A, with_todo=False)
    env = dict(_env(awf_env, "crash", tmp_path / "state1"))
    env["AWF_SHIM_SUPERVISOR_MODE"] = "plan-todo"
    run1 = run_awf(
        awf_bin, ["start", "--auto"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    assert run1.returncode != 0, "crashed worker must stop the run"
    snap = _snapshot(proj)
    assert snap.is_file(), "unpinned launch must capture the snapshot"

    snap.write_bytes(b"\x00\xff\xfe broken yaml [\x80")

    env2 = _env(awf_env, "do-step", tmp_path / "state2")
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=env2, input_data=b"", timeout=180,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, (
        f"byte-corrupted snapshot must degrade, not refuse. "
        f"rc={run2.returncode}\n{out2}"
    )
    assert "Pipeline complete!" in out2, out2
    assert re.search(r"snapshot .+ is unreadable", out2, re.IGNORECASE), out2
    assert "implement" in snap.read_text(encoding="utf-8"), "not re-captured"


@pytest.mark.timeout(300)
def test_structurally_invalid_snapshot_degrades_like_missing(tmp_path, awf_bin, awf_env):
    """QA review (M3.3) — FINDING: a structurally invalid snapshot (valid
    YAML, but a stage is not a mapping) crashes the launch with AwfApiError
    instead of degrading like the missing or byte-corrupted cases. The
    docstring's "A snapshot that fails to load degrades the same way" is
    not upheld — the unit dies on its own shadow file. Red until the fix
    pass wraps the snapshot load in the same degrade path."""
    proj = _make_project(tmp_path, PIPELINE_A, with_todo=False)
    env = dict(_env(awf_env, "crash", tmp_path / "state1"))
    env["AWF_SHIM_SUPERVISOR_MODE"] = "plan-todo"
    run1 = run_awf(
        awf_bin, ["start", "--auto"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    assert run1.returncode != 0, "crashed worker must stop the run"
    snap = _snapshot(proj)
    assert snap.is_file(), "unpinned launch must capture the snapshot"

    snap.write_text("name: default\nstages:\n  - just a string\n", encoding="utf-8")

    env2 = _env(awf_env, "do-step", tmp_path / "state2")
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=env2, input_data=b"", timeout=180,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, (
        f"structurally invalid snapshot must degrade, not refuse. "
        f"rc={run2.returncode}\n{out2}"
    )
    assert "Pipeline complete!" in out2, out2
