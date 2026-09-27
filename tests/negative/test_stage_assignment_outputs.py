"""ORCH M3.2 (контракт забега и поручения): поручение в промпте,
объявленные выходы, лимит повтора на экземпляр стадии.

Дефект (воспроизведён): схема пайплайна (ALLOWED_STAGE_KEYS, A-06)
отвергает ключи ``input``/``output`` как неизвестные, execute-промпт
не содержит поручение стадии, а движок переходит к следующей роли по
DONE-сигналу, не проверяя, что объявленный выход стадии существует и
свежий — непустой Git diff считался выполненной работой.

Инварианты (TODO-0136):
1. Промпт execute-стадии содержит её ``task`` (и объявленные
   ``input``/``output``, если заданы); без ``task`` промпт прежний
   (back-compat). ``input``/``output`` — относительные пути проекта.
2. Перед переходом к следующей стадии движок проверяет объявленный
   ``output``: существует и свежий (mtime >= старт стадии); иначе —
   штатный путь провала стадии (policy ``on_blocked``), не тихий
   переход. Сообщение называет стадию и ожидаемый выход.
3. ``max_retries`` — на экземпляр стадии (id), не на роль: две стадии
   одной роли не делят счётчик.
4. Стадии без объявленных выходов — прежнее поведение (back-compat;
   e2e salvage-сценарий зелёный).
"""
from __future__ import annotations

import os
import subprocess
import time as _time
from pathlib import Path

import pytest
from conftest import _git_init, run_awf

import awf.pipeline_engine as engine
from awf.api._errors import AwfApiError
from awf.pipeline import Stage, load_stages

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

CONFIG_YAML = (
    "project:\n"
    "  name: m32-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

# Stage declares task + input + output; the declared output IS the file
# the do-step shim writes, so a fresh output lets the stage advance.
PROMPT_PIPELINE = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "draft"\n'
    '    role: "worker"\n'
    '    task: "Write the draft note"\n'
    '    input: "docs/brief.md"\n'
    '    output: "src/shim-TODO-0001-work.txt"\n'
    '    on_blocked: "stop"\n'
    '  - id: "polish"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)

# The declared output is a file the shim NEVER writes: DONE signal alone
# must not advance the stage.
MISSING_OUTPUT_PIPELINE = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "draft"\n'
    '    role: "worker"\n'
    '    task: "Write the draft note"\n'
    '    output: "src/declared-output.txt"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def _make_project(tmp_path: Path, pipeline_yaml: str) -> Path:
    """Throwaway git project: .agentic skeleton + pipeline + TODO-0001."""
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff"):
        (ag / sub).mkdir(parents=True)
    # .agentic/ is runtime data — without this the engine's own artifacts
    # look like untracked "work" (NEG-4 B1 surface, same as W6).
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    (ag / "pipelines" / "default.yaml").write_text(pipeline_yaml, encoding="utf-8")
    (ag / "roles" / "worker.md").write_text("# Worker\nExecute the TODO.\n", encoding="utf-8")
    (ag / "roles" / "supervisor.md").write_text("# Supervisor\nPlan and verify.\n", encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    inbox = ag / "inbox"
    (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub task\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
    return proj


def _env(awf_env: dict, mode: str, state_dir: Path) -> dict:
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = mode
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _worker_prompts(state_dir: Path) -> list[str]:
    """Full prompts of the worker invocations, in invocation order."""
    log = state_dir / "invocation.log"
    texts: list[str] = []
    n = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        n += 1
        parts = line.split()
        if parts[1] == "worker":
            p = state_dir / f"prompt-{n}-worker.txt"
            assert p.is_file(), f"shim did not save the prompt of invocation {n}"
            texts.append(p.read_text(encoding="utf-8"))
    return texts


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


# ── prove_red 1: промпт execute-стадии несёт поручение ────────────────────


@pytest.mark.timeout(240)
def test_stage_prompt_carries_task(tmp_path, awf_bin, awf_env):
    """ORCH M3.2.1: промпт execute-стадии содержит task, объявленные
    input/output; стадия без task — промпт прежний (back-compat)."""
    proj = _make_project(tmp_path, PROMPT_PIPELINE)
    state1 = tmp_path / "state1"
    env = _env(awf_env, "do-step", state1)

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")
    assert run.returncode == 0, f"run rc={run.returncode}\n{out}"
    assert "Pipeline complete!" in out, out

    prompts = _worker_prompts(state1)
    assert len(prompts) == 2, f"expected 2 worker invocations, got {len(prompts)}"
    draft_prompt, polish_prompt = prompts

    assert "Stage assignment" in draft_prompt, draft_prompt
    assert "Write the draft note" in draft_prompt, draft_prompt
    assert "docs/brief.md" in draft_prompt, draft_prompt
    assert "src/shim-TODO-0001-work.txt" in draft_prompt, draft_prompt
    # back-compat: no task → the prompt stays as before (invariant 1)
    assert "Stage assignment" not in polish_prompt, polish_prompt


# ── prove_red 2: объявленный выход не дан — не переходим ─────────────────


@pytest.mark.timeout(240)
def test_missing_declared_output_does_not_advance(tmp_path, awf_bin, awf_env):
    """ORCH M3.2.2: DONE-сигнал без объявленного выхода — стадия не
    выполнила поручение: штатный путь провала (on_blocked: stop), не
    тихий переход; сообщение называет стадию и ожидаемый выход."""
    proj = _make_project(tmp_path, MISSING_OUTPUT_PIPELINE)
    state1 = tmp_path / "state1"
    env = _env(awf_env, "do-step", state1)

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")

    assert run.returncode != 0, f"run rc={run.returncode}\n{out}"
    # штатный путь провала стадии (policy on_blocked: stop), не переход
    assert "Pipeline stopped by policy." in out, out
    # сообщение называет стадию и ожидаемый выход
    assert "Stage 'draft' did not fulfill its assignment" in out, out
    assert "src/declared-output.txt" in out, out
    # verify-стадия не запускалась
    assert not any(
        c[1] == "supervisor" and c[3] == "awf-supervisor-verify"
        for c in _invocations(state1)
    ), f"verify stage ran: {_invocations(state1)}"
    # юнит остаётся активным: не заархивирован, не закоммичен
    assert (proj / ".agentic" / "inbox" / "TODO-0001.md").is_file(), "TODO archived"
    assert not (proj / ".agentic" / "done" / "TODO-0001").exists(), "TODO committed"
    assert _state(proj).get("stage_name") == "draft", _state(proj)


# ── свежий, а не просто существующий ─────────────────────────────────────


@pytest.mark.timeout(240)
def test_stale_declared_output_does_not_advance(tmp_path, awf_bin, awf_env):
    """ORCH M3.2.2: выход, оставшийся от предыдущей стадии (mtime раньше
    старта стадии), не считается выполненным поручением."""
    proj = _make_project(tmp_path, MISSING_OUTPUT_PIPELINE)
    leftover = proj / "src" / "declared-output.txt"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("leftover from a previous stage\n", encoding="utf-8")
    old = _time.time() - 3600
    os.utime(leftover, (old, old))
    state1 = tmp_path / "state1"
    env = _env(awf_env, "do-step", state1)

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=180,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")

    assert run.returncode != 0, f"run rc={run.returncode}\n{out}"
    assert "Pipeline stopped by policy." in out, out
    assert "Stage 'draft' did not fulfill its assignment" in out, out
    assert "stale" in out, out
    assert (proj / ".agentic" / "inbox" / "TODO-0001.md").is_file(), "TODO archived"


# ── инвариант 3: лимит повтора — на экземпляр стадии, не на роль ─────────


def _escalate_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    (proj / ".agentic" / "inbox" / "TODO-0001.ready").write_text("", encoding="utf-8")
    (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
    return proj


def test_retry_budget_is_per_stage_instance(tmp_path, monkeypatch):
    """ORCH M3.2.3: две стадии одной роли — два независимых бюджета
    max_retries: исчерпание бюджета первой не трогает счётчик второй."""
    proj = _escalate_project(tmp_path)
    logs = proj / ".agentic" / "logs"
    qa_a = Stage(name="qa-one", role="qa", kind="execute", max_retries=1)
    monkeypatch.setattr(
        engine, "_run_supervisor_stage", lambda *a, **kw: "ACK-TODO-0001"
    )
    retry_counts = [0, 0, 0, 0]

    res1 = engine._handle_escalate(
        proj, logs, "qa-one", "TODO-0001", auto=False, stage=qa_a,
        retry_counts=retry_counts, stage_idx=1, pipeline_name=None,
    )
    assert res1 == (1, "TODO-0001", 0)
    assert retry_counts[1] == 1

    # budget exhausted: the same stage gets the stop, not a new budget
    res2 = engine._handle_escalate(
        proj, logs, "qa-one", "TODO-0001", auto=False, stage=qa_a,
        retry_counts=retry_counts, stage_idx=1, pipeline_name=None,
    )
    assert res2[2] == 1, f"expected stop on exhausted budget, got {res2}"
    # the same-role sibling stage keeps its untouched budget
    assert retry_counts[2] == 0


# ── модель стадии: input/output — относительные пути проекта ─────────────


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pipeline.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_declared_input_output_loads(tmp_path):
    """ORCH M3.2.1: input/output доступны на Stage и сохраняются
    сквозь load; без ключей — пустые строки."""
    stages = load_stages(_write(
        tmp_path,
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - id: draft\n    role: writer\n"
        '    task: "draft it"\n    input: "outline.md"\n'
        '    output: "draft.md"\n'
        "  - id: final_edit\n    role: editor\n"
        "  - name: verify\n    role: supervisor\n",
    ))
    assert stages[1].input == "outline.md"
    assert stages[1].output == "draft.md"
    assert stages[2].input == ""
    assert stages[2].output == ""


@pytest.mark.parametrize("key", ["input", "output"])
def test_absolute_path_rejected(tmp_path, key):
    """ORCH M3.2.1: input/output — относительные пути проекта; абсолютный
    путь — понятный отказ на загрузке."""
    for value in ("/etc/passwd", "~/.secret"):
        p = _write(
            tmp_path,
            f"stages:\n  - id: draft\n    role: writer\n    {key}: {value}\n"
            "  - name: verify\n    role: supervisor\n",
        )
        with pytest.raises(AwfApiError, match=f"'{key}'"):
            load_stages(p)


@pytest.mark.parametrize("key", ["input", "output"])
def test_traversal_path_rejected(tmp_path, key):
    for value in ("../x.md", "a/../../x.md"):
        p = _write(
            tmp_path,
            f'stages:\n  - id: draft\n    role: writer\n    {key}: "{value}"\n'
            "  - name: verify\n    role: supervisor\n",
        )
        with pytest.raises(AwfApiError, match=f"'{key}'"):
            load_stages(p)


def test_write_path_roundtrip_input_output(tmp_path):
    """ORCH M3.2.1: write-путь — тот же ruleset; input/output попадают в
    YAML и грузятся обратно."""
    from awf import api

    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    api.write_pipeline(
        proj,
        "p",
        [
            {"role": "supervisor", "name": "plan"},
            {
                "role": "writer", "id": "draft", "task": "draft it",
                "input": "outline.md", "output": "draft.md",
            },
            {"role": "supervisor", "name": "verify"},
        ],
    )
    stages = load_stages(proj / ".agentic" / "pipelines" / "p.yaml")
    assert stages[1].input == "outline.md"
    assert stages[1].output == "draft.md"
