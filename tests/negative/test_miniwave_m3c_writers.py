"""M3c (TODO-0234): писатели под ``.agentic`` — через единый managed-барьер.

Один класс S9-01: симлинк ``.agentic`` или его компонента уводит запись
``config.yaml`` / ``supervisor.md`` / роли / ``pipelines/*.yaml`` /
``plan.md`` / evidence / правки TODO за пределы проекта. Раньше эти
писатели шли через голый ``atomic_write_text`` (rename в symlink-каталоге),
теперь — ``managed_write_text``: no-follow по всем компонентам от корня
проекта, отказ ДО первого байта.

(a) на КАЖДОЕ семейство (7) — симлинк → отказ, внешний канарейка цел,
    целевой файл снаружи не создан/не изменён;
(b) нормальный путь — файл пишется, формат/содержимое прежние;
(c) для ``write_pipeline`` барьер стоит перед переносом старого файла в
    ``.bak`` — иначе канарейка «переезжает» до отказа.

Все проекты — disposable в ``tmp_path``; канарейки лежат ВНЕ проекта.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

T = "TODO-0001"


# ─── fixtures ───────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in (
        "inbox", "outbox", "context", "logs", "done", "state",
        "roles", "pipelines", "phases",
    ):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project: demo\nmodels: {}\n", encoding="utf-8"
    )
    (project / ".agentic" / "roles" / "supervisor.md").write_text(
        "# supervisor\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.md").write_text(
        "# task\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text(
        "", encoding="utf-8"
    )
    return project


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=target.is_dir())


def _outside(tmp_path: Path, name: str = "outside") -> Path:
    outside = tmp_path / name
    outside.mkdir()
    (outside / "canary.txt").write_text("keep me", encoding="utf-8")
    return outside


def _evidence():
    from awf.candidate_verify import CandidateEvidence

    return CandidateEvidence(
        todo_id=T,
        candidate_tree="tree",
        contract_digest="contract",
        environment_digest="env",
        generation=1,
        unit_revision="r1",
        files_digest="files",
        head_expected="head",
    )


# ─── (a): отказ по каждому семейству ────────────────────────────────────


def test_config_write_refuses_symlinked_agentic(tmp_path):
    from awf.api import setup
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    canary = outside / "config.yaml"
    canary.write_text("project: outside\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        setup.save_context_and_instructions(project, context_message="hi")

    assert canary.read_text(encoding="utf-8") == "project: outside\n"
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_supervisor_write_refuses_symlinked_roles(tmp_path):
    from awf.api import setup
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_roles")
    canary = outside / "supervisor.md"
    canary.write_text("# outside supervisor\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "roles", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        setup.save_context_and_instructions(
            project, supervisor_instructions="obey the plan"
        )

    assert canary.read_text(encoding="utf-8") == "# outside supervisor\n"
    assert sorted(p.name for p in outside.iterdir()) == [
        "canary.txt", "supervisor.md"
    ]


def test_role_write_refuses_symlinked_roles(tmp_path):
    from awf.api import roles
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_roles")
    _replace_with_symlink(project / ".agentic" / "roles", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        roles.add_role(project, "auditor")

    assert not (outside / "auditor.md").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_pipeline_write_refuses_symlinked_pipelines(tmp_path):
    from awf.api import pipelines
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_pipelines")
    _replace_with_symlink(project / ".agentic" / "pipelines", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipelines.write_pipeline(project, "test", [{"role": "worker"}])

    assert not (outside / "test.yaml").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_setup_pipeline_barrier_runs_before_backup_rename(tmp_path):
    """Барьер — ДО ``rename`` старого файла в ``.bak``.

    Если проверка стоит только на записи, канарейка снаружи успевает
    «переехать» в ``.bak`` — это уже изменение чужого файла.
    """
    from awf.api import setup
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_pipelines")
    canary = outside / "default.yaml"
    canary.write_text("name: default\nstages: []\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "pipelines", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        setup.write_pipeline([{"agent": "developer"}], project)

    assert canary.read_text(encoding="utf-8") == "name: default\nstages: []\n"
    assert not (outside / "default.yaml.bak").exists()


def test_plan_write_refuses_symlinked_phases(tmp_path):
    from awf.api import planning
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_phases")
    canary = outside / "plan.md"
    canary.write_text("# old plan\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "phases", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        planning.apply_increment_plan(project, "# new plan\n")

    assert canary.read_text(encoding="utf-8") == "# old plan\n"


def test_evidence_write_refuses_symlinked_context(tmp_path):
    from awf.candidate_verify import write_evidence

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / f"CANDIDATE-{T}.json"
    canary.write_text("{}\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(OSError, match="unsafe managed path"):
        write_evidence(project, _evidence())

    assert canary.read_text(encoding="utf-8") == "{}\n"


def test_todo_edit_refuses_symlinked_inbox(tmp_path):
    from awf.api import hygiene
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_inbox")
    canary = outside / f"{T}.md"
    canary.write_text("# outside task\n", encoding="utf-8")
    (outside / f"{T}.ready").write_text("", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.update_todo(project, T, content="# new task\n")

    assert canary.read_text(encoding="utf-8") == "# outside task\n"


# ─── (b): нормальный путь — формат прежний ──────────────────────────────


def test_normal_config_and_supervisor_format(tmp_path):
    from awf.api import setup

    project = _project(tmp_path)
    assert setup.save_context_and_instructions(
        project,
        context_message="hello",
        supervisor_instructions="obey the plan",
    )

    config = yaml.safe_load(
        (project / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    )
    assert config["context"]["message"] == "hello"
    assert config["supervisor"]["instructions"] == "obey the plan"

    supervisor = (project / ".agentic" / "roles" / "supervisor.md").read_text(
        encoding="utf-8"
    )
    assert supervisor.startswith("# supervisor\n")
    assert "<!-- awf:ui2:start -->" in supervisor
    assert "## Project context (from user, UI-2)\n\nhello" in supervisor
    assert "<!-- awf:ui3:start -->" in supervisor
    assert (
        "## Additional supervisor instructions (from user, UI-3)\n\n"
        "obey the plan" in supervisor
    )


def test_normal_role_format(tmp_path):
    from awf.api import roles

    project = _project(tmp_path)
    roles.add_role(project, "auditor", description="checks the work")

    text = (project / ".agentic" / "roles" / "auditor.md").read_text(
        encoding="utf-8"
    )
    assert text.startswith("# ROLE: auditor\n")
    assert "**Role:** checks the work" in text
    assert "## 5. Prohibitions" in text


def test_normal_pipeline_format(tmp_path):
    from awf.api import pipelines

    project = _project(tmp_path)
    pipelines.write_pipeline(project, "test", [{"role": "worker"}])

    data = yaml.safe_load(
        (project / ".agentic" / "pipelines" / "test.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert data["name"] == "test"
    assert data["stages"] == [{"role": "worker", "name": "worker"}]


def test_normal_plan_format(tmp_path):
    from awf.api import planning

    project = _project(tmp_path)
    planning.apply_increment_plan(project, "# Plan\n\n- step\n")

    text = (project / ".agentic" / "phases" / "plan.md").read_text(
        encoding="utf-8"
    )
    assert text.startswith("---\n")
    assert "updated_at:" in text
    assert text.endswith("# Plan\n\n- step\n\n")


def test_normal_evidence_format(tmp_path):
    from awf import candidate_verify

    project = _project(tmp_path)
    ev = _evidence()
    candidate_verify.write_evidence(project, ev)

    path = candidate_verify.evidence_path(project, T)
    assert path == project / ".agentic" / "context" / f"CANDIDATE-{T}.json"
    assert path.read_text(encoding="utf-8") == ev.to_json()
    assert candidate_verify.read_evidence(project, T) == ev


def test_normal_todo_edit_format(tmp_path):
    from awf.api import hygiene

    project = _project(tmp_path)
    hygiene.update_todo(project, T, content="# new task\n", reason="reword")

    inbox = project / ".agentic" / "inbox"
    assert (inbox / f"{T}.md").read_text(encoding="utf-8") == "# new task\n"
    backups = list((project / ".agentic" / "context").glob(f"{T}.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# task\n"


def test_todo_edit_backup_refuses_symlinked_context(tmp_path):
    """REVIEW (architect): бэкап правки TODO — та же семья, что и сама
    правка. Симлинк ``context`` раньше уводил старый текст наружу, хотя
    основная запись уже стояла за барьером."""
    from awf.api import hygiene
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / f"{T}.md.bak-canary"
    canary.write_text("keep me", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.update_todo(project, T, content="# new task\n")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert sorted(p.name for p in outside.iterdir()) == sorted(
        ["canary.txt", f"{T}.md.bak-canary"]
    )
    assert (project / ".agentic" / "inbox" / f"{T}.md").read_text(
        encoding="utf-8"
    ) == "# task\n"


# ─── REVIEW K1: checkpoint TODO edit — тот же барьер ────────────────────


def test_checkpoint_saved_edit_refuses_symlinked_inbox(tmp_path):
    """K1: сохранённое решение ``edit`` правит TODO мимо барьера.

    Раньше здесь стоял голый ``atomic_write_text``: симлинк ``inbox``
    уводил перезапись TODO наружу. Теперь — ``managed_write_text``.
    """
    import json

    from awf import plan_checkpoint
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_inbox")
    canary = outside / f"{T}.md"
    canary.write_text("# outside task\n", encoding="utf-8")
    decision = project / ".agentic" / "context" / f"CHECKPOINT-{T}.json"
    decision.write_text(
        json.dumps({"decision": "edit", "content": "# edited\n"}),
        encoding="utf-8",
    )
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(ManagedPathError, match="symlink"):
        plan_checkpoint._apply_pending_checkpoint_decision(
            T,
            project / ".agentic" / "inbox" / f"{T}.md",
            project,
            project / ".agentic" / "logs",
        )

    assert canary.read_text(encoding="utf-8") == "# outside task\n"


def test_checkpoint_form_edit_refuses_symlinked_inbox(tmp_path, monkeypatch):
    """K1: правка TODO из самой checkpoint-формы — тоже за барьером."""
    import re
    import tempfile
    import threading
    import time
    import urllib.request

    from awf import plan_checkpoint
    from awf.infra.managed_fs import ManagedPathError

    isolated = tmp_path / "ckpt-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
    monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_inbox")
    canary = outside / f"{T}.md"
    canary.write_text("# outside task\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)
    (project / ".agentic" / "phases" / "plan.md").write_text(
        "- [ ] step\n", encoding="utf-8"
    )

    real_start = plan_checkpoint._start_checkpoint_server

    def capturing_start(port, decision_holder, edited_holder, **kw):
        server = real_start(port, decision_holder, edited_holder, **kw)

        def _fire():
            time.sleep(0.2)
            forms = sorted(
                Path(tempfile.gettempdir()).glob(f"awf-checkpoint-{T}-*.html")
            )
            token = re.search(
                r'formToken = "([^"]+)"', forms[-1].read_text(encoding="utf-8")
            ).group(1)
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                data=b"decision=edit&edited_content=%23+edited&token=" + token.encode(),
                timeout=2,
            ).read()

        threading.Thread(target=_fire, daemon=True).start()
        return server

    monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)

    with pytest.raises(ManagedPathError, match="symlink"):
        plan_checkpoint.run_plan_checkpoint(
            T,
            project,
            config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=5,
        )

    assert canary.read_text(encoding="utf-8") == "# outside task\n"
    assert {p.name for p in outside.iterdir()} == {"canary.txt", f"{T}.md"}


def test_adopt_role_draft_refuses_symlinked_draft_dir(tmp_path):
    """REVIEW (architect): adopt unlinked the candidate after the copy.

    A symlinked ``roles/draft`` made that unlink delete an outside file —
    the preflight now refuses before the read and the write.
    """
    from awf.api import roles
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_draft")
    canary = outside / "auditor.md"
    canary.write_text("keep me", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "roles" / "draft", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        roles.adopt_role_draft(project, "auditor")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (project / ".agentic" / "roles" / "auditor.md").exists()


def test_checkpoint_hash_cache_refuses_symlinked_context(tmp_path):
    """REVIEW (architect): the P1 "same plan" cache sits in ``context`` too.

    ``run_plan_checkpoint`` wrote it with a bare ``write_text`` — a
    symlinked ``context`` sent the cache outside the project. The cache is
    best-effort: the refusal must not raise, only skip the write.
    """
    from awf import plan_checkpoint

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / "checkpoint-approved.hash"
    canary.write_text(f"{T}:deadbeef\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    plan_checkpoint._persist_content_hash(
        project / ".agentic" / "context" / "checkpoint-approved.hash",
        T,
        "0badc0de",
    )

    assert canary.read_text(encoding="utf-8") == f"{T}:deadbeef\n"
    assert sorted(p.name for p in outside.iterdir()) == [
        "canary.txt",
        "checkpoint-approved.hash",
    ]


# ─── REVIEW K2: RUN-EVIDENCE + VERIFIED — под барьером ─────────────────


def test_run_evidence_write_refuses_symlinked_context(tmp_path):
    """K2: ``RUN-EVIDENCE`` писался голым ``atomic_write_text``."""
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / f"RUN-EVIDENCE-{T}.md"
    canary.write_text("# outside evidence\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.approve_commit(project, T, evidence="pytest -q → ok; verdict: approve")

    assert canary.read_text(encoding="utf-8") == "# outside evidence\n"


def test_verified_fingerprint_write_refuses_symlinked_context(tmp_path, monkeypatch):
    """K2: ``mkdir`` перед ``VERIFIED`` шагал по симлинку ``context``."""
    from awf import git_utils
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    _replace_with_symlink(project / ".agentic" / "context", outside)
    fp = "a" * 64
    monkeypatch.setattr(git_utils, "tree_fingerprint", lambda _p: fp)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.approve_commit(project, T, verified_sha=fp)

    assert not (outside / f"VERIFIED-{T}.sha").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


# ─── REVIEW K3: отказ барьера в ветке бэкапа — ветка жива ──────────────


def test_config_backup_barrier_refusal_keeps_update(tmp_path):
    """K3: ``except OSError`` не ловил ``AwfApiError`` барьера."""
    from awf.api import setup

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_bak")
    canary = outside / "config.yaml.bak"
    canary.write_text("project: outside\n", encoding="utf-8")
    (project / ".agentic" / "config.yaml.bak").symlink_to(canary)
    (project / ".agentic" / "config.yaml").write_text(
        "project: demo\nmodels: {}\n", encoding="utf-8"
    )

    assert setup.update_config_role_mapping(
        [{"role": "worker"}], project
    )

    data = yaml.safe_load(
        (project / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    )
    assert data["models"]["worker"]["agent_name"] == "worker"
    assert canary.read_text(encoding="utf-8") == "project: outside\n"
    assert sorted(p.name for p in outside.iterdir()) == [
        "canary.txt", "config.yaml.bak"
    ]


# ─── REVIEW K4: mode semantics — 0600 поверх 0644 ──────────────────────


def test_managed_writer_sets_0600_on_existing_role(tmp_path):
    """K4: managed-писатель ставит 0o600 поверх существующего 0o644."""
    import os

    from awf.api import roles

    project = _project(tmp_path)
    role = project / ".agentic" / "roles" / "auditor.md"
    role.write_text("# old\n", encoding="utf-8")
    os.chmod(role, 0o644)

    roles.add_role(project, "auditor", force=True)

    assert role.stat().st_mode & 0o777 == 0o600


# ─── REVIEW K5: платформенная политика — fail-closed для setup ─────────


def test_workspace_api_refuses_on_non_posix(tmp_path, monkeypatch):
    """K5: ``require_posix`` — обязателен для API-записей состояния."""
    from awf.api import setup
    from awf.api._errors import AwfApiError
    from awf.infra import managed_fs

    project = _project(tmp_path)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(AwfApiError, match="POSIX"):
        setup.write_pipeline([{"agent": "developer"}], project)

    assert not (project / ".agentic" / "pipelines" / "default.yaml").exists()


# ─── REVIEW виток 3: C1/C2 — разрушительный init и скелет ───────────────


def _git_project(tmp_path: Path) -> Path:
    import subprocess

    project = _project(tmp_path)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


def test_force_init_refuses_symlinked_agentic(tmp_path):
    """C1: force-init сносит runtime только после no-follow preflight.

    Симлинк ``.agentic`` наружу раньше уводил ``rmtree`` на чужие
    каталоги. Отказ, внешние файлы целы.
    """
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path)
    (outside / "logs").mkdir()
    (outside / "logs" / "keep.log").write_text("keep log", encoding="utf-8")
    _replace_with_symlink(project / ".agentic", outside)

    with pytest.raises(AwfApiError, match="unsafe"):
        lifecycle.init_project(project, project_name="demo", force=True)

    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"
    assert (outside / "logs" / "keep.log").read_text(encoding="utf-8") == "keep log"


def test_force_init_refuses_symlinked_roles(tmp_path):
    """C1: симлинк одного runtime-каталога (``roles``) — тоже отказ."""
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path, "outside_roles")
    (outside / "draft").mkdir()
    (outside / "draft" / "keep.md").write_text("keep draft", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "roles", outside)

    with pytest.raises(AwfApiError, match="unsafe runtime path"):
        lifecycle.init_project(project, project_name="demo", force=True)

    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"
    assert (outside / "draft" / "keep.md").read_text(encoding="utf-8") == "keep draft"


def test_reinit_refuses_symlinked_agentic_before_mkdir(tmp_path):
    """C2 (QA F1): скелет не создаётся снаружи — preflight ДО mkdir."""
    import subprocess

    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic", outside)
    subprocess.run(["git", "init", "-q", str(project)], check=True)

    with pytest.raises(AwfApiError, match="unsafe skeleton path"):
        lifecycle.init_project(project, project_name="demo")

    leaked = sorted(p.name for p in outside.iterdir() if p.name != "canary.txt")
    assert leaked == [], f"init created outside the project: {leaked}"


# ─── REVIEW виток 3: C3/C4 — checkpoint-решение и evidence ─────────────


def test_checkpoint_decision_refuses_symlinked_context(tmp_path):
    """C3 (QA F2): решение формы не уходит наружу через симлинк context."""
    from awf import plan_checkpoint

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / f"CHECKPOINT-{T}.json"
    canary.write_text("keep me", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    plan_checkpoint._persist_checkpoint_decision(
        project / ".agentic" / "context" / f"CHECKPOINT-{T}.json",
        T,
        "edit",
        "# secret edited text\n",
        None,
    )

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert {p.name for p in outside.iterdir()} == {
        "canary.txt", f"CHECKPOINT-{T}.json"
    }


def test_write_evidence_accepts_relative_project_dir(tmp_path, monkeypatch):
    """C4: относительный корень — нормальный проект, не ложный отказ."""
    from awf.candidate_verify import evidence_path, read_evidence, write_evidence

    project = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    write_evidence(Path("proj"), _evidence())

    assert evidence_path(project, T).is_file()
    assert read_evidence(Path("proj"), T) is not None


def test_write_evidence_refuses_symlinked_context_relative_root(
    tmp_path, monkeypatch
):
    """C4: относительный корень не ослабляет барьер — симлинк отказ."""
    from awf.candidate_verify import write_evidence

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_context")
    canary = outside / f"CANDIDATE-{T}.json"
    canary.write_text("{}\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(OSError, match="unsafe managed path"):
        write_evidence(Path("proj"), _evidence())

    assert canary.read_text(encoding="utf-8") == "{}\n"


def test_api_barrier_root_follows_leaf(tmp_path, monkeypatch):
    """REVIEW (architect): у барьера API один корень — из листа.

    ``paths.*`` резолвит проект, а вызывающий мог передать относительную
    запись. Отдельный ``project_dir`` был второй записью того же корня:
    пара «резолвный лист + относительный корень» читалась как выход за
    проект (класс C4). Корень теперь выводится из листа, поэтому пара
    согласована по построению и симлинк-отказ не ослаблен.
    """
    from awf import paths
    from awf.api._errors import AwfApiError
    from awf.api._helpers import managed_write_api_text

    project = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    leaf = paths.context_dir(Path("proj")) / "note.md"

    managed_write_api_text(leaf, "hello\n", operation="write note")
    assert leaf.read_text(encoding="utf-8") == "hello\n"

    outside = _outside(tmp_path, "outside_context_leaf")
    canary = outside / "note.md"
    canary.write_text("keep me\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        managed_write_api_text(leaf, "escaped\n", operation="write note")
    assert canary.read_text(encoding="utf-8") == "keep me\n"


# ─── REVIEW виток 4: M1/M2 — один preflight init + doctrine ─────────────


def test_force_init_refuses_symlinked_phases_before_wipe(tmp_path):
    """M1: симлинк ``phases`` отказывает ДО сноса runtime.

    ``phases`` не входит в ``INIT_RUNTIME_DIRS``: раньше preflight скелета
    шёл после ``_clean_runtime``, и live inbox/logs сначала терялись, а
    отказ приходил уже по пустому проекту (QA Q1).
    """
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    (project / ".agentic" / "logs" / "awf.log").write_text(
        "log\n", encoding="utf-8"
    )
    outside = _outside(tmp_path, "outside_phases")
    (outside / "phases").mkdir()
    _replace_with_symlink(
        project / ".agentic" / "phases", outside / "phases"
    )

    with pytest.raises(AwfApiError, match="unsafe"):
        lifecycle.init_project(project, project_name="demo", force=True)

    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file(), (
        "force-init refusal wiped the live inbox task"
    )
    assert (project / ".agentic" / "logs" / "awf.log").is_file(), (
        "force-init refusal wiped the logs"
    )


def test_force_init_refuses_symlinked_pipelines_before_wipe(tmp_path):
    """M1: симлинк ``pipelines`` — тот же единый preflight, ничего не снесено."""
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path, "outside_pipelines")
    (outside / "pipelines").mkdir()
    _replace_with_symlink(
        project / ".agentic" / "pipelines", outside / "pipelines"
    )

    with pytest.raises(AwfApiError, match="unsafe"):
        lifecycle.init_project(project, project_name="demo", force=True)

    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file(), (
        "force-init refusal wiped the live inbox task"
    )
    assert {p.name for p in (outside / "pipelines").iterdir()} == set()


def test_init_refuses_symlinked_doctrine(tmp_path):
    """M2: симлинк ``doctrine`` → отказ, снаружи ни одного шаблона."""
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path, "outside_doctrine")
    _replace_with_symlink(project / ".agentic" / "doctrine", outside)

    with pytest.raises(AwfApiError, match="unsafe"):
        lifecycle.init_project(project, project_name="demo")

    assert {p.name for p in outside.iterdir()} == {"canary.txt"}


def test_init_seeds_doctrine_with_template_content(tmp_path):
    """M2: нормальный путь не изменился — шаблоны на месте, байты прежние."""
    from awf import doctrine
    from awf.api import lifecycle

    project = _git_project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: demo\nmodels: {}\n", encoding="utf-8"
    )
    result = lifecycle.init_project(project, project_name="demo")

    expected = sorted(
        p.name for p in doctrine.BUILTIN_DOCTRINE_DIR.glob("*.md")
    )
    assert expected, "built-in doctrine templates are missing"
    ddir = project / ".agentic" / "doctrine"
    assert sorted(p.name for p in ddir.iterdir()) == expected
    for name in expected:
        assert (ddir / name).read_text(encoding="utf-8") == (
            doctrine.BUILTIN_DOCTRINE_DIR / name
        ).read_text(encoding="utf-8")
        assert f".agentic/doctrine/{name}" in result.created_files


# ─── REVIEW виток 5: Q3 — preflight по ветке, не по объединению ─────────


def test_reinit_tolerates_symlinked_runtime_dir_it_does_not_write(tmp_path):
    """Q3: non-force re-init не отказывает на симлинке, который не пишет.

    Ветка создаёт только ОТСУТСТВУЮЩИЕ каталоги скелета; живой симлинк
    ``logs`` в цикле пропускается по ``exists()``. Preflight всего плана
    падал «unsafe skeleton path» на рутинном ``awf init``, хотя ни одной
    записи в ``logs`` не было бы. Проверяем: init проходит, снаружи
    по-прежнему только канарейка, живая задача на месте.
    """
    from awf.api import lifecycle

    project = _git_project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: demo\nmodels: {}\n", encoding="utf-8"
    )
    outside = _outside(tmp_path, "outside_logs")
    _replace_with_symlink(project / ".agentic" / "logs", outside)

    result = lifecycle.init_project(project, project_name="demo")

    assert result.project_name == "demo"
    assert {p.name for p in outside.iterdir()} == {"canary.txt"}, (
        "re-init wrote into the symlinked logs directory"
    )
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()


def test_force_init_refuses_symlinked_logs_before_wipe(tmp_path):
    """Q3b: обратная сторона — force на том же симлинке отказывает ДО сноса."""
    from awf.api import lifecycle
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path, "outside_logs_force")
    _replace_with_symlink(project / ".agentic" / "logs", outside)

    with pytest.raises(AwfApiError, match="unsafe"):
        lifecycle.init_project(project, project_name="demo", force=True)

    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file(), (
        "force-init refusal wiped the live inbox task"
    )


# ─── REVIEW (architect, виток 5): режим плана — один у артефакта ────────


def test_every_plan_writer_uses_owner_mode(tmp_path):
    """План — один артефакт: у него один режим у всех писателей.

    ``apply_increment_plan`` и ``mark_plan_step_done`` раньше расходились
    (0600 против 0644 у checkpoint-формы): режим зависел от того, какой
    путь записал последним. Теперь все берут ``MODE_OWNER_EDITABLE``.
    """
    import os

    from awf import plan_progress
    from awf.api import planning

    project = _project(tmp_path)
    plan = project / ".agentic" / "phases" / "plan.md"

    (project / ".agentic" / "inbox" / f"{T}.md").write_text(
        "---\nstep_id: 1\n---\n\n# task\n", encoding="utf-8"
    )
    plan.write_text(
        "# Plan\n\n- [ ] Step 1: first\n", encoding="utf-8"
    )
    os.chmod(plan, 0o600)

    assert plan_progress.mark_plan_step_done(
        project, T, project / ".agentic" / "logs"
    )
    assert plan.stat().st_mode & 0o777 == 0o644
    assert "- [x] Step 1: first" in plan.read_text(encoding="utf-8")

    planning.apply_increment_plan(project, "# Plan 2\n")
    assert plan.stat().st_mode & 0o777 == 0o644


# ─── REVIEW виток 6: Q4/Q4b — один базис у корня и поверхности ──────────


def test_barrier_puts_surfaces_on_the_root_basis(tmp_path, monkeypatch):
    """Q4: поверхность и корень сравниваются на одном базисе.

    Форма входа → исход:

    - относительный корень (``Path("proj")``) → проходит;
    - абсолютный путь через симлинк-родителя до корня → проходит;
    - лист-симлинк наружу → отказ, канарейка цела;
    - родитель-симлинк наружу → отказ.
    """
    from awf.api import _helpers
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    _helpers.refuse_unsafe_surfaces(
        Path("proj"), "write pipeline",
        Path("proj/.agentic/pipelines/default.yaml"),
    )

    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    _helpers.refuse_unsafe_surfaces(
        link, "write pipeline",
        link / ".agentic" / "pipelines" / "default.yaml",
    )

    outside = _outside(tmp_path, "outside_pipelines")
    canary = outside / "default.yaml"
    canary.write_text("canary\n", encoding="utf-8")
    leaf = project / ".agentic" / "pipelines" / "default.yaml"
    leaf.symlink_to(canary)
    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _helpers.refuse_unsafe_surfaces(project, "write pipeline", leaf)
    assert canary.read_text(encoding="utf-8") == "canary\n"

    leaf.unlink()
    _replace_with_symlink(project / ".agentic" / "pipelines", outside)
    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _helpers.refuse_unsafe_surfaces(project, "write pipeline", leaf)


def test_setup_pipelines_accept_relative_and_linked_roots(tmp_path, monkeypatch):
    """Q4/Q4b на публичном входе: относительный и симлинк-родитель корни.

    На baseline 60b80ea обе записи проходили; юнит добавил барьер и начал
    отказывать на нормальных формах корня. Теперь корень и поверхность
    согласованы, запись снова проходит, а файл лежит в реальном проекте.
    """
    from awf.api import setup

    project = _git_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    default = setup.write_pipeline([{"agent": "developer"}], Path("proj"))
    assert default is not None and default.is_file()

    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    quick = setup.write_quick_pipeline([{"agent": "developer"}], link)
    assert quick is not None and quick.is_file()
    assert quick.resolve().parent == (project / ".agentic" / "pipelines").resolve()


# ─── REVIEW виток 7: Q5/Q6 — сырой обход, не только резолвный ───────────


def test_barrier_refuses_internal_symlink_component(tmp_path):
    """Q5: внутренний симлинк-компонент — отказ, живая роль цела.

    ``roles/draft -> roles`` резолвится ВНУТРЬ корня, поэтому containment
    его пропускал, а ``discard_role_draft`` затем делал ``unlink`` по
    сырому пути и удалял живую роль. Второй проход по СЫРЫМ компонентам
    от сырого корня возвращает baseline-отказ.
    """
    from awf.api import _helpers, roles
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    live = project / ".agentic" / "roles" / "live-role.md"
    live.write_text("# live role\n", encoding="utf-8")
    draft = project / ".agentic" / "roles" / "draft"
    draft.symlink_to(project / ".agentic" / "roles", target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _helpers.refuse_unsafe_surfaces(
            project, "discard role draft", draft / "live-role.md"
        )
    with pytest.raises(AwfApiError, match="unsafe managed path"):
        roles.discard_role_draft(project, "live-role")
    assert live.is_file(), "discard followed an internal symlink and deleted a live role"
    assert live.read_text(encoding="utf-8") == "# live role\n"


def test_barrier_symlink_loop_is_the_api_refusal(tmp_path):
    """Q6: петля симлинков — ``AwfApiError``, а не ``RuntimeError``.

    ``pipelines -> pipelines`` раньше давал трейсбек из ``resolve()``:
    запись fail-closed, но тип ошибки выпадал из матрицы API. Сырой проход
    видит симлинк через ``lstat`` до любого резолва, резолв защищён.
    """
    from awf.api import setup
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    pipelines = project / ".agentic" / "pipelines"
    pipelines.rmdir()
    pipelines.symlink_to(pipelines)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        setup.write_pipeline([{"agent": "developer"}], project)


def test_setup_pipeline_ignores_bak_symlink_when_no_target(tmp_path):
    """Q3 class (REVIEW architect): барьер проверяет то, что ветка трогает.

    ``.bak`` — поверхность только когда существующий файл переносится в
    сторону. Без цели (``default.yaml`` нет) ``rename`` не выполняется, и
    симлинк на месте ``.bak`` не должен отказывать нормальной записи.
    """
    from awf.api import setup

    project = _project(tmp_path)
    outside = _outside(tmp_path, "outside_bak")
    canary = outside / "default.yaml.bak"
    canary.write_text("keep me\n", encoding="utf-8")
    (project / ".agentic" / "pipelines" / "default.yaml.bak").symlink_to(canary)

    written = setup.write_pipeline([{"agent": "developer"}], project)

    assert written is not None and written.is_file()
    assert written.name == "default.yaml"
    assert canary.read_text(encoding="utf-8") == "keep me\n"
    assert sorted(p.name for p in outside.iterdir()) == [
        "canary.txt", "default.yaml.bak"
    ]


# ─── REVIEW виток 7 (QA): две проверки барьера и базис у писателей ──────


def test_barrier_containment_catches_dotdot_surface(tmp_path):
    """Q4 (QA): containment — единственная проверка, видящая путь с ``..``.

    Сырой проход идёт по ЛЕКСИЧЕСКИМ компонентам: ``.agentic/../..`` не
    содержит симлинков, поэтому он проходит. Выход за корень видит только
    второй проход (резолвные родители). Убери его — разрушающая операция
    сделает ``unlink``/``move`` по внешнему пути.
    """
    from awf.api import _helpers
    from awf.api._errors import AwfApiError

    project = _git_project(tmp_path)
    outside = _outside(tmp_path)
    canary = outside / "canary.txt"
    surface = project / ".agentic" / ".." / ".." / outside.name / canary.name

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _helpers.refuse_unsafe_surfaces(project, "remove", surface)

    assert canary.read_text(encoding="utf-8") == "keep me"


def test_managed_writers_accept_project_through_symlinked_parent(tmp_path):
    """Q4b (QA): симлинк-РОДИТЕЛЬ проекта — не побег для всех писателей.

    Q4 чинил ложный отказ на относительном/симлинк-корне в ``setup``. Тот
    же базис нужен каждому managed-писателю: задаче, роли, evidence,
    config/supervisor. Если барьер снова начнёт сравнивать резолвный лист
    с нерезолвным корнем, эти записи откажут на нормальном проекте.
    """
    from awf import candidate_verify
    from awf.api import hygiene, roles, setup

    project = _git_project(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)

    hygiene.update_todo(link, T, content="# via link\n")
    roles.add_role(link, "auditor")
    candidate_verify.write_evidence(link, _evidence())
    setup.save_context_and_instructions(link, context_message="hi")

    assert (project / ".agentic" / "inbox" / f"{T}.md").read_text(
        encoding="utf-8"
    ) == "# via link\n"
    assert (project / ".agentic" / "roles" / "auditor.md").is_file()
    assert (project / ".agentic" / "context" / f"CANDIDATE-{T}.json").is_file()
    assert "message: hi" in (project / ".agentic" / "config.yaml").read_text(
        encoding="utf-8"
    )
