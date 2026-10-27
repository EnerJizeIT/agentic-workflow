"""M6: системность ролей и пайплайнов (требования TODO-0230).

Красный до, зелёный после:
(a) from_skill-роль несёт блок «дисциплина доставки инвариантов»
    (маркеры, идемпотентно), пресет — тоже;
(b) refresh ролей: изменённый скилл обновляет тело, AWF/BD-31/дисциплина
    и пользовательские строки сохраняются; без скилла — пропуск;
    повтор — no-op;
(c) шаблоны full/code-health грузятся pipeline-loader'ом, форма
    project-setup предлагает вариант «полная проверка»;
(d) шаблон supervisor несёт каноны (4 секции);
(e) docs/practices.md — не меньше 5 практик с инцидентами.

Плюс доборы из M1: один источник шаблонов, suite-скрипт читает
`verification.test_cmd`, force-init не перезаписывает чужой скрипт,
правило канон-прогона появляется при материализации ролей.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from awf import api
from awf.pipeline import load_stages

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "awf" / "templates"
FORM = (
    REPO_ROOT
    / "agent_workflow_ui"
    / "src"
    / "agent_workflow_ui"
    / "render"
    / "default_templates"
    / "project-setup.html.j2"
)

DISCIPLINE_MARKER = "<!-- awf-discipline:start -->"
DISCIPLINE_END = "<!-- awf-discipline:end -->"
SUITE_MARKER = "<!-- awf-suite-canon:start -->"
AWF_HEADING = "## AWF-OVERRIDE: awf pipeline overrides"
BD31_HEADING = "## BD-31: Pipeline-specific disambiguation"


def _write_skill(project: Path, name: str, body: str) -> Path:
    skill = project / ".opencode" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill


def _role(project: Path, name: str) -> Path:
    return project / ".agentic" / "roles" / f"{name}.md"


# ─── (a) дисциплина в материализации ────────────────────────────────────


def test_from_skill_role_has_discipline_block_idempotent(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Roles")
    _write_skill(tmp_git_repo, "skill-alpha", "# Alpha\n\nbody v1")

    result = api.add_role(tmp_git_repo, "alpha", from_skill="skill-alpha")
    content = Path(result.role_file).read_text(encoding="utf-8")
    assert DISCIPLINE_MARKER in content
    assert "таблица поверхностей" in content.lower() or "таблиц" in content.lower()
    assert "поверхност" in content.lower()

    api.add_role(tmp_git_repo, "alpha", from_skill="skill-alpha", force=True)
    again = _role(tmp_git_repo, "alpha").read_text(encoding="utf-8")
    assert again.count(DISCIPLINE_MARKER) == 1
    assert "body v1" in again


def test_preset_roles_carry_discipline_block(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Preset")
    _write_skill(tmp_git_repo, "agent-system-analyst", "# Analyst\n")
    result = api.seed_roles(tmp_git_repo, preset="office")
    assert any(row["role"] == "system-analyst" for row in result.created)
    content = _role(tmp_git_repo, "system-analyst").read_text(encoding="utf-8")
    assert DISCIPLINE_MARKER in content


# ─── (b) refresh ролей ──────────────────────────────────────────────────


def test_refresh_updates_skill_body_and_keeps_local_blocks(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Refresh")
    _write_skill(tmp_git_repo, "skill-beta", "# Beta\n\nBODY-V1")
    api.add_role(tmp_git_repo, "beta", from_skill="skill-beta")

    path = _role(tmp_git_repo, "beta")
    original = path.read_text(encoding="utf-8")
    local = (
        "\n\n## BD-31: Pipeline-specific disambiguation (added by awf analyze-roles)\n"
        "LOCAL-BD31\n\nUSER-NOTE: my local tweak\n"
    )
    path.write_text(original.rstrip() + local, encoding="utf-8")

    _write_skill(tmp_git_repo, "skill-beta", "# Beta\n\nBODY-V2")
    result = api.add_role(tmp_git_repo, "beta", refresh=True)

    refreshed = path.read_text(encoding="utf-8")
    assert result.refresh_status == "updated", result.as_dict()
    assert "BODY-V2" in refreshed and "BODY-V1" not in refreshed
    assert "LOCAL-BD31" in refreshed
    assert "USER-NOTE: my local tweak" in refreshed
    assert refreshed.count(AWF_HEADING) == 1
    assert refreshed.count(DISCIPLINE_MARKER) == 1
    assert SUITE_MARKER in refreshed

    second = api.add_role(tmp_git_repo, "beta", refresh=True)
    assert second.refresh_status in ("unchanged", "skipped")
    assert path.read_text(encoding="utf-8") == refreshed


def test_refresh_skips_with_reason_without_skill(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Skip")
    api.add_role(tmp_git_repo, "plain", description="plain")

    result = api.add_role(tmp_git_repo, "plain", refresh=True)
    assert result.refresh_status == "skipped"
    assert result.refresh_reason

    _write_skill(tmp_git_repo, "gone", "# Gone\n")
    api.add_role(tmp_git_repo, "gone-role", from_skill="gone")
    (_role(tmp_git_repo, "gone-role"))
    (tmp_git_repo / ".opencode" / "skills" / "gone" / "SKILL.md").unlink()

    missing = api.add_role(tmp_git_repo, "gone-role", refresh=True)
    assert missing.refresh_status == "skipped"
    assert "not found" in missing.refresh_reason.lower()


def test_refresh_is_a_noop_when_only_the_provenance_date_is_old(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6NoopDay")
    _write_skill(tmp_git_repo, "skill-day", "# Day\n\nBODY")
    api.add_role(tmp_git_repo, "day", from_skill="skill-day")

    path = _role(tmp_git_repo, "day")
    text = path.read_text(encoding="utf-8")
    old = re.sub(
        r" on \d{4}-\d{2}-\d{2} -->", " on 2020-01-01 -->", text, count=1
    )
    assert old != text
    path.write_text(old, encoding="utf-8")

    result = api.add_role(tmp_git_repo, "day", refresh=True)
    assert result.refresh_status == "unchanged", result.as_dict()
    assert path.read_text(encoding="utf-8") == old, (
        "refresh переписал файл только из-за даты в provenance-строке"
    )


def test_refresh_keeps_a_model_that_lives_only_in_the_role_file(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6ModelFile")
    _write_skill(tmp_git_repo, "skill-model", "# Model\n\nBODY")
    api.add_role(
        tmp_git_repo,
        "draft-model",
        from_skill="skill-model",
        model="acme/model-x",
        draft=True,
    )
    api.adopt_role_draft(tmp_git_repo, "draft-model")

    api.add_role(tmp_git_repo, "draft-model", refresh=True)

    assert "acme/model-x" in _role(tmp_git_repo, "draft-model").read_text(
        encoding="utf-8"
    )


def test_refresh_with_explicit_model_writes_the_config(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6ModelCfg")
    _write_skill(tmp_git_repo, "skill-cfg", "# Cfg\n\nBODY")
    api.add_role(tmp_git_repo, "cfg-role", from_skill="skill-cfg")

    result = api.add_role(
        tmp_git_repo, "cfg-role", refresh=True, model="acme/model-y"
    )
    assert result.refresh_status == "updated", result.as_dict()
    assert result.model_config == "models.cfg-role.model"
    assert "`acme/model-y`" in _role(tmp_git_repo, "cfg-role").read_text(
        encoding="utf-8"
    )
    config_text = (tmp_git_repo / ".agentic" / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert "acme/model-y" in config_text

    again = api.add_role(tmp_git_repo, "cfg-role", refresh=True)
    assert again.refresh_status == "unchanged", again.as_dict()
    assert again.model == "acme/model-y"


def test_refresh_plus_draft_is_refused_before_writing(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6RefreshDraft")
    _write_skill(tmp_git_repo, "skill-live", "# Live\n\nBODY-V1")
    api.add_role(tmp_git_repo, "live", from_skill="skill-live")
    path = _role(tmp_git_repo, "live")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(api.AwfApiError):
        api.add_role(tmp_git_repo, "live", refresh=True, draft=True)

    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_git_repo / ".agentic" / "roles" / "draft" / "live.md").exists()


def test_refresh_refuses_from_skill_instead_of_ignoring_it(tmp_git_repo):
    """refresh uses the recorded provenance skill; from_skill would be lost."""
    api.init_project(tmp_git_repo, project_name="M6RefreshFrom")
    _write_skill(tmp_git_repo, "skill-a", "# A\n\nBODY-A")
    _write_skill(tmp_git_repo, "skill-b", "# B\n\nBODY-B")
    api.add_role(tmp_git_repo, "from-role", from_skill="skill-a")
    path = _role(tmp_git_repo, "from-role")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(api.AwfApiError):
        api.add_role(
            tmp_git_repo, "from-role", refresh=True, from_skill="skill-b"
        )

    assert path.read_text(encoding="utf-8") == before
    assert "BODY-A" in before


def test_refresh_replaces_legacy_blocks_instead_of_duplicating(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Legacy")
    _write_skill(tmp_git_repo, "skill-legacy", "# Legacy\n\nBODY-V2")
    path = _role(tmp_git_repo, "legacy")
    path.write_text(
        "<!-- copied from skill `skill-legacy` (/tmp/skill-legacy) on 2026-01-01 -->\n"
        "# Legacy\n\nBODY-V1\n\n**Модель:** `vllm/llm`\n\n---\n\n"
        "## AWF-OVERRIDE: awf pipeline overrides (added by awf add_role)\n"
        "AWF-TEXT\n\n---\n\n"
        "## Дисциплина доставки инвариантов (супервизор, 18.10)\n"
        "OLD-DISCIPLINE-TEXT\n\n---\n\n"
        "## Канонический полный сьют (M1)\nOLD-CANON-TEXT\n",
        encoding="utf-8",
    )

    result = api.add_role(tmp_git_repo, "legacy", refresh=True)
    assert result.refresh_status == "updated", result.as_dict()
    refreshed = path.read_text(encoding="utf-8")
    assert "BODY-V2" in refreshed
    assert "OLD-DISCIPLINE-TEXT" not in refreshed
    assert "OLD-CANON-TEXT" not in refreshed
    assert refreshed.count("## Дисциплина доставки инвариантов") == 1
    assert "AWF-TEXT" in refreshed


# ─── (c) шаблоны пайплайнов + форма ─────────────────────────────────────


@pytest.mark.parametrize(
    "name,roles",
    [
        (
            "full",
            [
                "supervisor",
                "agent-implementer",
                "agent-architect-review",
                "agent-qa-review",
                "agent-implementer",
                "agent-project-auditor",
                "supervisor",
            ],
        ),
        (
            "code-health",
            [
                "supervisor",
                "agent-architect-review",
                "agent-refactoring-specialist",
                "agent-implementer",
                "supervisor",
            ],
        ),
    ],
)
def test_builtin_pipeline_templates_load(name, roles):
    path = TEMPLATES / "pipelines" / f"{name}.yaml"
    assert path.is_file(), f"нет шаблона пайплайна: {path}"
    stages = load_stages(path)
    assert [s.role for s in stages] == roles


def test_setup_form_offers_full_check_variant():
    from awf.api.setup import BUILTIN_PIPELINES

    text = FORM.read_text(encoding="utf-8")
    assert 'name="flow_variant"' in text
    assert 'value="{{ variant }}"' in text
    assert "Полная проверка" in BUILTIN_PIPELINES["full"]


def test_apply_project_setup_writes_full_variant(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Full")
    result = api.apply_project_setup(
        tmp_git_repo,
        team=[{"agent": "agent-implementer"}],
        flow_variant="full",
    )
    assert result.pipeline_file
    stages = load_stages(result.pipeline_file)
    assert [s.role for s in stages] == [
        "supervisor",
        "agent-implementer",
        "agent-architect-review",
        "agent-qa-review",
        "agent-implementer",
        "agent-project-auditor",
        "supervisor",
    ]


def test_flow_variant_warns_about_unresolved_stage_roles(
    tmp_git_repo, monkeypatch
):
    """A starter template must not silently write a pipeline that cannot run."""
    api.init_project(tmp_git_repo, project_name="M6Missing")
    monkeypatch.setattr(
        "awf.api.setup._role_resolves", lambda role, project: False
    )
    result = api.apply_project_setup(
        tmp_git_repo,
        team=[{"agent": "agent-implementer"}],
        flow_variant="code-health",
    )
    joined = " ".join(result.warnings)
    for role in ("agent-architect-review", "agent-refactoring-specialist"):
        assert role in joined, joined


def test_flow_variant_preview_is_the_written_file(tmp_git_repo):
    """REPORTS33: the form preview must show the variant's real text."""
    from agent_workflow_ui.tools.forms import _populate_flow_variant_previews

    api.init_project(tmp_git_repo, project_name="M6Preview")
    preview = _populate_flow_variant_previews(tmp_git_repo)
    assert preview.get("full"), preview
    assert preview["full"] == api.setup.pipeline_variant_yaml_text("full")

    result = api.apply_project_setup(
        tmp_git_repo,
        team=[{"agent": "agent-implementer"}],
        flow_variant="full",
    )
    written = Path(result.pipeline_file).read_text(encoding="utf-8")
    assert written == preview["full"], (
        "превью варианта разошлось с тем, что записал write_pipeline"
    )


def test_unknown_flow_variant_with_empty_team_is_not_silent(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6BadVariant")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="bogus"
    )
    assert any("bogus" in w for w in result.warnings), result.warnings


def test_flow_variant_preview_matches_write_with_a_custom_default_pipeline(
    tmp_git_repo,
):
    """M6: preview and write resolve the active pipeline name the same way.

    A project may declare ``default_pipeline``; both the form preview and
    the written file must carry THAT name, or "what you see" is not what
    lands. One resolver (``awf.pipeline.active_pipeline_name``) backs both.
    """
    from agent_workflow_ui.tools.forms import _populate_flow_variant_previews

    api.init_project(tmp_git_repo, project_name="M6CustomName")
    cfg = tmp_git_repo / ".agentic" / "config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").rstrip("\n")
        + "\ndefault_pipeline: custom-flow\n",
        encoding="utf-8",
    )

    preview = _populate_flow_variant_previews(tmp_git_repo)
    result = api.apply_project_setup(tmp_git_repo, team=[], flow_variant="full")
    assert Path(result.pipeline_file).name == "custom-flow.yaml"
    written = Path(result.pipeline_file).read_text(encoding="utf-8")
    assert preview["full"] == written, (
        "превью варианта и записанный файл разошлись по имени/тексту"
    )


# ─── (d) каноны супервизора ─────────────────────────────────────────────


def test_supervisor_template_has_four_canon_sections():
    text = (TEMPLATES / "roles" / "supervisor.md").read_text(encoding="utf-8")
    for marker in (
        "Правило остановки",
        "Шаблон REVIEW-фикса",
        "Дисциплина переносов",
        "Когда эскалировать владельцу",
    ):
        assert marker in text, f"в шаблоне supervisor нет канона: {marker}"


# ─── (e) каталог практик ────────────────────────────────────────────────


def test_practices_catalog_has_five_incident_entries():
    text = (REPO_ROOT / "docs" / "practices.md").read_text(encoding="utf-8")
    assert text.count("**Практика.**") >= 5
    assert text.count("**Инцидент.**") >= 5


# ─── M1 доборы ──────────────────────────────────────────────────────────


def test_role_templates_have_single_source():
    """Y1 (REVIEW виток 10): the package directory is the only source.

    The repo-root ``templates/`` symlink forced the engine to stage paths
    "beyond a symbolic link" (approve was impossible). It is gone; a
    checkout and a wheel both read ``awf/templates``.
    """
    repo_templates = REPO_ROOT / "templates"
    assert not repo_templates.exists()
    assert not repo_templates.is_symlink()
    assert TEMPLATES.is_dir(), "package templates missing"


def test_suite_script_uses_project_test_cmd(tmp_git_repo, tmp_path):
    api.init_project(tmp_git_repo, project_name="M6Suite")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    assert script.is_file()

    counter = tmp_path / "counter.txt"
    stub = tmp_path / "js-suite.sh"
    stub.write_text(
        f'#!/usr/bin/env bash\necho run >> "{counter}"\necho "1 passed"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    cfg = tmp_git_repo / ".agentic" / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^  test_cmd:.*$", f"  test_cmd: '{stub}'", text)
    cfg.write_text(text, encoding="utf-8")

    # Deterministic (REVIEW R1): no awf on PATH, the interpreter is the
    # test's own venv, AWF_SUITE_CMD cannot shadow the config.
    env = {k: v for k, v in os.environ.items() if k != "AWF_SUITE_CMD"}
    env["PATH"] = "/usr/bin:/bin"
    env["AWF_PYTHON"] = sys.executable
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_git_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert counter.read_text(encoding="utf-8").count("run") == 1, (
        "suite-canonical.sh проигнорировал verification.test_cmd"
    )


def test_suite_script_survives_a_broken_awf_on_path(tmp_git_repo, tmp_path):
    """REVIEW R1: a broken entry point on PATH must fall through."""
    api.init_project(tmp_git_repo, project_name="M6BrokenAwf")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"

    bin_dir = tmp_path / "broken-bin"
    bin_dir.mkdir()
    broken = bin_dir / "awf"
    broken.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'ModuleNotFoundError: No module named awf' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    broken.chmod(0o755)

    counter = tmp_path / "counter-broken.txt"
    stub = tmp_path / "js-suite-broken.sh"
    stub.write_text(
        f'#!/usr/bin/env bash\necho run >> "{counter}"\necho "1 passed"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    cfg = tmp_git_repo / ".agentic" / "config.yaml"
    cfg.write_text(
        re.sub(
            r"(?m)^  test_cmd:.*$",
            f"  test_cmd: '{stub}'",
            cfg.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if k != "AWF_SUITE_CMD"}
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["AWF_PYTHON"] = sys.executable
    proc = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_git_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert counter.read_text(encoding="utf-8").count("run") == 1
    assert "ModuleNotFoundError" not in proc.stderr, (
        "скрипт выбрал битый awf с PATH вместо python -m awf"
    )


def test_force_init_keeps_user_suite_script(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="M6Force")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    script.write_text("#!/usr/bin/env bash\necho mine\n", encoding="utf-8")

    # U4 (REVIEW виток 6): the path stays in the plan, but NOT among the
    # rewritten files — init only creates it when missing.
    from awf.api import _destructive

    assert "scripts/suite-canonical.sh" not in _destructive.INIT_REWRITTEN_FILES
    preview = api.preview_destruction(tmp_git_repo, kind="init", mode="force")
    suite_lines = [
        line
        for line in preview.scope
        if line.startswith("scripts/suite-canonical.sh")
    ]
    assert suite_lines, preview.scope
    assert "kept as-is" in suite_lines[0], suite_lines

    api.init_project(tmp_git_repo, project_name="M6Force", force=True)
    assert script.read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho mine\n"


def test_materialized_code_reading_role_does_not_run_tests(tmp_git_repo):
    """RB-1: ревьюер читает артефакт, а не платит вторым прогоном сьюта."""
    api.init_project(tmp_git_repo, project_name="M6CanonRead")
    _write_skill(tmp_git_repo, "agent-code-reviewer", "# Reviewer\n\nreads code")
    api.add_role(tmp_git_repo, "code-reviewer", from_skill="agent-code-reviewer")

    content = _role(tmp_git_repo, "code-reviewer").read_text(encoding="utf-8")
    assert content.count(SUITE_MARKER) == 1
    assert "Тесты в этой роли НЕ запускаются" in content
    assert "ОДИН канонический прогон" not in content
    assert ".agentic/context/SUITE-<tree-sha>-<cmd8>.json" in content


@pytest.mark.parametrize(
    ("role", "policy"),
    [
        ("agent-qa-review", "qa"),
        ("agent-qa-table", "qa"),
        ("agent-implementer", "implementer"),
        ("agent-weak-llm-implementer", "implementer"),
        ("agent-architect-review", "code-reading"),
        ("agent-architector", "code-reading"),
        ("agent-code-reviewer", "code-reading"),
        ("agent-requirements-reviewer", "code-reading"),
        ("agent-project-auditor", "auditor"),
        ("agent-auditor-table", "auditor"),
        ("agent-refactoring-specialist", "targeted"),
        ("", "targeted"),
    ],
)
def test_role_test_policy_mapping(role, policy):
    """RB-1: тип роли определяет прогон — qa первым, иначе review съел бы QA."""
    from awf.api.roles import role_test_policy

    assert role_test_policy(role) == policy


@pytest.mark.parametrize(
    ("role", "fragment"),
    [
        ("agent-qa-review", "ОДИН канонический прогон"),
        ("agent-implementer", "только целевые"),
        ("agent-project-auditor", "артефакт QA"),
        ("agent-architect-review", "НЕ запускаются"),
        ("agent-refactoring-specialist", "только целевые проверки изменённой поверхности"),
    ],
)
def test_materialized_role_gets_its_own_test_policy(
    tmp_git_repo, role, fragment
):
    """RB-1: материализованная роль несёт политику своего типа, маркер один."""
    api.init_project(tmp_git_repo, project_name=f"M6Policy{role}")
    api.add_role(tmp_git_repo, role)

    content = _role(tmp_git_repo, role).read_text(encoding="utf-8")
    assert content.count(SUITE_MARKER) == 1, role
    assert fragment in content, role


def test_refresh_migrates_the_old_suite_block_in_place(tmp_git_repo):
    """RB-1: старый безусловный канон заменяется, второй маркер не растёт."""
    legacy_body = (
        "Полный сьют — ОДИН канонический прогон на attempt: "
        "`scripts/suite-canonical.sh`."
    )
    api.init_project(tmp_git_repo, project_name="M6PolicyMigrate")
    _write_skill(tmp_git_repo, "skill-review", "# Review\n\nreads code")
    api.add_role(tmp_git_repo, "code-reviewer", from_skill="skill-review")
    path = _role(tmp_git_repo, "code-reviewer")
    text = path.read_text(encoding="utf-8")
    current_heading = "## Политика тестов (added by awf add_role)"
    assert current_heading in text
    body_start = text.index(SUITE_MARKER) + len(SUITE_MARKER)
    body_end = text.index("<!-- awf-suite-canon:end -->")
    legacy = (
        text[:body_start]
        + "\n## Канон полного прогона (added by awf add_role)\n\n"
        + legacy_body
        + "\n"
        + text[body_end:]
    )
    path.write_text(legacy, encoding="utf-8")

    result = api.add_role(
        tmp_git_repo, "code-reviewer", refresh=True
    )
    assert result.refresh_status == "updated", result.as_dict()

    refreshed = path.read_text(encoding="utf-8")
    assert refreshed.count(SUITE_MARKER) == 1
    assert "Тесты в этой роли НЕ запускаются" in refreshed
    assert "Канон полного прогона" not in refreshed



def test_migration_keeps_a_user_note_after_the_legacy_block(tmp_git_repo):
    """RB-1: миграция старого блока не съедает пользовательский хвост."""
    legacy_body = (
        "Полный сьют — ОДИН канонический прогон на attempt: "
        "`scripts/suite-canonical.sh`."
    )
    api.init_project(tmp_git_repo, project_name="M6PolicyMigrateTail")
    _write_skill(tmp_git_repo, "skill-tail", "# Review\n\nreads code")
    api.add_role(tmp_git_repo, "code-reviewer", from_skill="skill-tail")
    path = _role(tmp_git_repo, "code-reviewer")
    text = path.read_text(encoding="utf-8")
    body_start = text.index(SUITE_MARKER) + len(SUITE_MARKER)
    body_end = text.index("<!-- awf-suite-canon:end -->")
    legacy = (
        text[:body_start]
        + "\n## Канон полного прогона (added by awf add_role)\n\n"
        + legacy_body
        + "\n"
        + text[body_end:]
    )
    path.write_text(
        legacy.rstrip() + "\n\nUSER-NOTE: keep me\n", encoding="utf-8"
    )

    result = api.add_role(tmp_git_repo, "code-reviewer", refresh=True)
    refreshed = path.read_text(encoding="utf-8")
    assert result.refresh_status == "updated", result.as_dict()
    assert refreshed.count(SUITE_MARKER) == 1
    assert "Тесты в этой роли НЕ запускаются" in refreshed
    assert "Канон полного прогона" not in refreshed
    assert "USER-NOTE: keep me" in refreshed


def test_draft_candidate_carries_the_role_policy(tmp_git_repo):
    """RB-1: draft идёт тем же путём — политика выбирается по имени роли."""
    api.init_project(tmp_git_repo, project_name="M6PolicyDraft")
    api.add_role(tmp_git_repo, "agent-code-reviewer", draft=True)

    draft = (
        tmp_git_repo / ".agentic" / "roles" / "draft" / "agent-code-reviewer.md"
    )
    content = draft.read_text(encoding="utf-8")
    assert content.count(SUITE_MARKER) == 1
    assert "Тесты в этой роли НЕ запускаются" in content
    assert "ОДИН канонический прогон" not in content
    assert not _role(tmp_git_repo, "agent-code-reviewer").exists()


# ─── REVIEW R2-R4 (виток 3) ─────────────────────────────────────────────


def test_flow_variant_needs_no_team(tmp_git_repo):
    """REVIEW R2: a variant is a complete composition — empty team is fine."""
    api.init_project(tmp_git_repo, project_name="M6VariantNoTeam")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="code-health"
    )
    assert result.pipeline_file, result.as_dict()
    stages = load_stages(result.pipeline_file)
    assert [s.role for s in stages] == [
        "supervisor",
        "agent-architect-review",
        "agent-refactoring-specialist",
        "agent-implementer",
        "supervisor",
    ]
    assert result.quick_pipeline_file is None
    assert any("quick pipeline" in w for w in result.warnings), result.warnings


def test_flow_variant_names_dropped_composition_knobs(tmp_git_repo):
    """REVIEW R2: stage_mode/primary_role/include_qa must not vanish silently."""
    api.init_project(tmp_git_repo, project_name="M6VariantKnobs")
    result = api.apply_project_setup(
        tmp_git_repo,
        team=[{"agent": "agent-implementer"}],
        flow_variant="full",
        stage_mode="single",
        include_qa=True,
    )
    joined = " ".join(result.warnings)
    assert "stage_mode" in joined and "not applied" in joined, result.warnings


def test_flow_variant_messages_differ_for_typo_and_broken_install(monkeypatch):
    """REVIEW R3: "no such variant" and "template not installed" differ."""
    from awf.api import setup as setup_api

    with pytest.raises(ValueError) as typo:
        setup_api._require_builtin_pipeline("bogus")
    assert "Unknown flow variant" in str(typo.value)

    monkeypatch.setattr(
        setup_api, "_builtin_pipeline_template", lambda name: None
    )
    with pytest.raises(ValueError) as broken:
        setup_api._require_builtin_pipeline("full")
    assert "not installed" in str(broken.value)
    assert str(broken.value) != str(typo.value)


def test_setup_form_renders_variant_radio_only_when_it_exists():
    """REVIEW R3/W5: the radio is driven by the registry data, not hardcoded."""
    text = FORM.read_text(encoding="utf-8")
    assert "{% for variant in (flow_variants or {}) %}" in text
    assert "'full'" not in text and "code-health" not in text
    # The submit guard allows a variant without a team (R2).
    assert "selectedFlowVariant()" in text


def test_refresh_applies_explicit_description(tmp_git_repo):
    """REVIEW R4: description is input, not a dropped argument."""
    api.init_project(tmp_git_repo, project_name="M6Desc")
    _write_skill(tmp_git_repo, "skill-desc", "# Desc\n\nBODY")
    api.add_role(tmp_git_repo, "desc-role", from_skill="skill-desc")

    result = api.add_role(
        tmp_git_repo,
        "desc-role",
        refresh=True,
        description="обновлённое описание роли",
    )
    assert result.refresh_status == "updated", result.as_dict()
    content = _role(tmp_git_repo, "desc-role").read_text(encoding="utf-8")
    assert "**Role:** обновлённое описание роли" in content
    assert content.count("**Role:**") == 1

    # A later refresh without a description keeps the line.
    api.add_role(tmp_git_repo, "desc-role", refresh=True)
    kept = _role(tmp_git_repo, "desc-role").read_text(encoding="utf-8")
    assert "**Role:** обновлённое описание роли" in kept


def test_refresh_force_rewrites_an_otherwise_unchanged_file(tmp_git_repo):
    """REVIEW R4: --force is an explicit rewrite, not a swallowed input."""
    api.init_project(tmp_git_repo, project_name="M6ForceRefresh")
    _write_skill(tmp_git_repo, "skill-force", "# Force\n\nBODY")
    api.add_role(tmp_git_repo, "force-role", from_skill="skill-force")

    path = _role(tmp_git_repo, "force-role")
    old = re.sub(
        r" on \d{4}-\d{2}-\d{2} -->", " on 2020-01-01 -->", path.read_text(
            encoding="utf-8"
        ), count=1
    )
    path.write_text(old, encoding="utf-8")

    result = api.add_role(tmp_git_repo, "force-role", refresh=True, force=True)
    assert result.refresh_status == "updated", result.as_dict()
    rewritten = path.read_text(encoding="utf-8")
    assert f"on {date.today().isoformat()} -->" in rewritten


# ─── REVIEW виток 6: U1-U4 ──────────────────────────────────────────────


def _rewrite_test_cmd(project: Path, value: str) -> None:
    cfg = project / ".agentic" / "config.yaml"
    cfg.write_text(
        re.sub(
            r"(?m)^  test_cmd:.*$",
            f"  test_cmd: '{value}'",
            cfg.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def _run_suite_script(project: Path, script: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AWF_SUITE_CMD"}
    env["PATH"] = "/usr/bin:/bin"
    env["AWF_PYTHON"] = sys.executable
    return subprocess.run(
        ["bash", str(script)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_suite_script_reports_an_empty_test_cmd(tmp_git_repo):
    """U2: an empty test_cmd is «not configured», not a red pytest run."""
    api.init_project(tmp_git_repo, project_name="M6SuiteEmpty")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    _rewrite_test_cmd(tmp_git_repo, "")

    proc = _run_suite_script(tmp_git_repo, script)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert (
        "suite not configured: set verification.test_cmd or AWF_SUITE_CMD"
        in proc.stderr
    ), proc.stderr


def test_suite_script_reads_the_command_from_the_given_project_dir(
    tmp_git_repo, tmp_path
):
    """The script cds to --project: config-get must not read the caller's cwd."""
    import json

    api.init_project(tmp_git_repo, project_name="M6SuiteCwd")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    _rewrite_test_cmd(tmp_git_repo, "true")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "AWF_SUITE_CMD"}
    env["PATH"] = "/usr/bin:/bin"
    env["AWF_PYTHON"] = sys.executable
    proc = subprocess.run(
        ["bash", str(script), "--project", str(tmp_git_repo)],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)

    artifacts = list((tmp_git_repo / ".agentic" / "context").glob("SUITE-*.json"))
    assert artifacts, proc.stdout
    data = json.loads(
        max(artifacts, key=lambda f: f.stat().st_mtime).read_text(encoding="utf-8")
    )
    assert data["command"] == "true", data
    assert data["exit"] == 0, data
    assert not (elsewhere / ".agentic").exists()


def test_config_get_prints_yaml_and_signals_a_missing_key(tmp_git_repo, capsys):
    """U3: a missing key exits 1; non-string values print as YAML."""
    from awf import cli

    api.init_project(tmp_git_repo, project_name="M6ConfigGet")

    rc = cli.main(
        ["config-get", "project.name", "--project-dir", str(tmp_git_repo)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "M6ConfigGet", out  # strings print raw, no quotes

    rc = cli.main(
        ["config-get", "models.supervisor", "--project-dir", str(tmp_git_repo)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "description: Current session model" in out, out
    assert "{" not in out, f"Python repr leaked instead of YAML: {out}"

    rc = cli.main(
        ["config-get", "no.such.key", "--project-dir", str(tmp_git_repo)]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert out == ""

    usage = (REPO_ROOT / "USAGE.md").read_text(encoding="utf-8")
    assert "awf config-get" in usage


def test_full_variant_materializes_its_roles(tmp_git_repo):
    """U1: a fresh project gets the role files the variant needs."""
    from awf.api import setup as setup_api

    api.init_project(tmp_git_repo, project_name="M6VarRoles")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="full"
    )
    assert result.pipeline_file, result.as_dict()

    roles_dir = tmp_git_repo / ".agentic" / "roles"
    stages = load_stages(result.pipeline_file)
    for stage in stages:
        assert setup_api._role_resolves(stage.role, tmp_git_repo), (
            f"вариант записал пайплайн, но роль {stage.role} не разрешается"
        )
    # U1(a): the package ships the three role templates the variants name.
    for role in (
        "agent-architect-review",
        "agent-project-auditor",
        "agent-refactoring-specialist",
    ):
        assert (TEMPLATES / "roles" / f"{role}.md").is_file(), role
    # Not in the global roles dir, so setup must have written it into the
    # project (the auditor may resolve globally on some machines —
    # resolution above already proves it runs there).
    assert (roles_dir / "agent-architect-review.md").is_file()
    assert not [w for w in result.warnings if "no role file" in w], (
        result.warnings
    )


def test_variant_refuses_a_role_without_a_template(tmp_git_repo, monkeypatch):
    """U1: if a role cannot be resolved even from the package — refuse."""
    from awf.api import setup as setup_api

    api.init_project(tmp_git_repo, project_name="M6VarNoRole")
    real = setup_api._builtin_role_template
    monkeypatch.setattr(
        setup_api,
        "_builtin_role_template",
        lambda role: (
            None
            if role == "agent-refactoring-specialist"
            else real(role)
        ),
    )

    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="code-health"
    )
    assert result.pipeline_file is None
    joined = " ".join(result.warnings)
    assert "agent-refactoring-specialist" in joined, joined
    assert "not installed" in joined, joined


def test_form_hides_a_variant_without_a_role_source(tmp_git_repo, monkeypatch):
    """U1: the radio disappears when the variant cannot be materialized."""
    from agent_workflow_ui.tools.forms import _populate_flow_variant_previews

    from awf.api import setup as setup_api

    api.init_project(tmp_git_repo, project_name="M6VarHidden")
    real = setup_api._builtin_role_template
    monkeypatch.setattr(
        setup_api,
        "_builtin_role_template",
        lambda role: (
            None
            if role == "agent-refactoring-specialist"
            else real(role)
        ),
    )

    preview = _populate_flow_variant_previews(tmp_git_repo)
    assert "full" in preview
    assert "code-health" not in preview, preview


def test_force_init_plan_marks_a_broken_suite_symlink(tmp_git_repo, tmp_path):
    """U4: a dangling leaf is named as broken, not as «will be rewritten»."""
    api.init_project(tmp_git_repo, project_name="M6BrokenLeaf")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    script.unlink()
    script.symlink_to(tmp_path / "gone")

    preview = api.preview_destruction(tmp_git_repo, kind="init", mode="force")
    suite_lines = [
        line
        for line in preview.scope
        if line.startswith("scripts/suite-canonical.sh")
    ]
    assert suite_lines, preview.scope
    assert "broken file/symlink" in suite_lines[0], suite_lines


# ─── REVIEW V4/V5/V6: вариант делает проект самодостаточным ─────────────


def test_variant_copies_roles_into_the_project_even_if_they_resolve_globally(
    tmp_git_repo, monkeypatch
):
    """V4 [A1]: a global role must not hide the missing project copy."""
    from awf.api import setup as setup_api

    api.init_project(tmp_git_repo, project_name="M6VarPortable")
    # The runtime order resolves globals; the project copy is what makes
    # the committed project portable — it must be written regardless.
    monkeypatch.setattr(setup_api, "_role_resolves", lambda role, project: True)

    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="full"
    )
    assert result.pipeline_file, result.as_dict()
    for role in (
        "agent-implementer",
        "agent-architect-review",
        "agent-qa-review",
        "agent-project-auditor",
    ):
        path = _role(tmp_git_repo, role)
        assert path.is_file(), f"нет проектной копии роли {role}"


def test_variant_does_not_overwrite_an_existing_project_role(tmp_git_repo):
    """V4: an existing project role is the owner's file — copy missing only."""
    api.init_project(tmp_git_repo, project_name="M6VarKeep")
    custom = _role(tmp_git_repo, "agent-implementer")
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("CUSTOM-IMPL: my protocol\n", encoding="utf-8")

    result = api.apply_project_setup(tmp_git_repo, team=[], flow_variant="full")
    assert result.pipeline_file, result.as_dict()

    text = custom.read_text(encoding="utf-8")
    assert text.startswith("CUSTOM-IMPL: my protocol\n"), text
    assert "## Порядок работы (обязательный)" not in text, (
        "setup перезаписал существующую проектную роль шаблоном пакета"
    )


def test_variant_roles_carry_the_managed_blocks(tmp_git_repo):
    """V5 [A2]: setup-materialized roles get the discipline/suite canon."""
    from awf.api import setup as setup_api

    api.init_project(tmp_git_repo, project_name="M6VarBlocks")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="full"
    )
    assert result.pipeline_file, result.as_dict()
    for role in setup_api._stage_roles(load_stages(result.pipeline_file)):
        text = _role(tmp_git_repo, role).read_text(encoding="utf-8")
        assert DISCIPLINE_MARKER in text, role
        assert SUITE_MARKER in text, role


def test_role_templates_no_longer_hand_write_the_managed_sections():
    """V5 [A2]: one owner of the text — the generator, not the templates."""
    for template in (TEMPLATES / "roles").glob("*.md"):
        text = template.read_text(encoding="utf-8")
        assert "<!-- M1-canonical-suite:start -->" not in text, template
        assert DISCIPLINE_MARKER not in text, template


def test_variant_roles_get_the_agent_name_mapping(tmp_git_repo):
    """V6 [A3]: variant roles are mapped in config like add_role roles."""
    import yaml

    api.init_project(tmp_git_repo, project_name="M6VarAgentName")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="full"
    )
    assert result.pipeline_file, result.as_dict()
    config = yaml.safe_load(
        (tmp_git_repo / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    )
    models = config.get("models") or {}
    for role in (
        "agent-implementer",
        "agent-architect-review",
        "agent-qa-review",
        "agent-project-auditor",
    ):
        assert (models.get(role) or {}).get("agent_name") == "worker", role


def test_pipeline_write_variant_writes_the_template_text(tmp_git_repo):
    """V8 [A5]: the variant is reachable from the CLI, byte-identical."""
    from awf import cli
    from awf.api.setup import pipeline_variant_yaml_text

    api.init_project(tmp_git_repo, project_name="M6PipelineVariant")
    rc = cli.main(
        [
            "pipeline-write",
            "audit",
            "--variant",
            "full",
            "--project-dir",
            str(tmp_git_repo),
        ]
    )
    assert rc == 0
    written = (
        tmp_git_repo / ".agentic" / "pipelines" / "audit.yaml"
    ).read_text(encoding="utf-8")
    assert written == pipeline_variant_yaml_text("full", name="audit")


def test_refresh_strips_a_blank_separated_known_discipline_block(tmp_git_repo):
    """V7 [A4]: a blank line between the intro and the bullets leaves no orphans."""
    api.init_project(tmp_git_repo, project_name="M6LegacyBlank")
    _write_skill(tmp_git_repo, "skill-blank", "# Blank\n\nBODY-V2")
    path = _role(tmp_git_repo, "blank")
    path.write_text(
        "<!-- copied from skill `skill-blank` (/tmp/skill-blank) on 2026-01-01 -->\n"
        "# Blank\n\nBODY-V1\n\n**Модель:** `vllm/llm`\n\n---\n\n"
        "## AWF-OVERRIDE: awf pipeline overrides (added by awf add_role)\n"
        "AWF-TEXT\n\n---\n\n"
        "## Дисциплина доставки инвариантов (супервизор, 18.10)\n"
        "Для изменений класса «байты/пути/идентичность/замки»: таблица.\n\n"
        "- implementer: таблица в DONE.\n"
        "- qa-review: сначала таблица.\n\n"
        "---\n\n"
        "## Канонический полный сьют (M1)\nOLD-CANON\n",
        encoding="utf-8",
    )

    result = api.add_role(tmp_git_repo, "blank", refresh=True)
    assert result.refresh_status == "updated", result.as_dict()
    refreshed = path.read_text(encoding="utf-8")
    assert "таблица в DONE" not in refreshed
    assert "сначала таблица" not in refreshed
    assert refreshed.count(DISCIPLINE_MARKER) == 1


# ─── REVIEW W1–W5: последний виток (утечка provenance, достижимость ролей,
#     флаг конфига, slug-гард, один реестр вариантов) ──────────────────────


def test_w1_refresh_refuses_a_provenance_name_that_is_not_a_slug(
    tmp_git_repo,
):
    """W1 [QA F7]: the provenance comment is repo-controlled data."""
    project = tmp_git_repo
    api.init_project(project, project_name="M6W1")
    secret = project.parent / "outside-w1" / "secret"
    secret.mkdir(parents=True)
    (secret / "SKILL.md").write_text(
        "# Skill\n\nLEAKED-SECRET-VALUE\n", encoding="utf-8"
    )
    role = _role(project, "evil")
    role.write_text(
        "<!-- copied from skill `" + str(secret) + "` (/x) on 2026-10-01 -->\n"
        "# Role\n\n"
        "**Модель:** не назначена (действует модель конфига по умолчанию)\n\n"
        f"{DISCIPLINE_MARKER}\n"
        "## Дисциплина доставки инвариантов (added by awf add_role)\n"
        "<!-- awf-discipline:end -->\n",
        encoding="utf-8",
    )

    result = api.add_role(project, "evil", refresh=True)

    assert result.refresh_status == "skipped", result.as_dict()
    assert "LEAKED-SECRET-VALUE" not in role.read_text(encoding="utf-8")


def test_w1_find_skill_file_rejects_a_non_slug_defense_in_depth(
    tmp_git_repo,
):
    """W1: the helper itself is fail-closed, not just the caller."""
    from awf.api._errors import AwfApiError
    from awf.api.roles import _find_skill_file

    api.init_project(tmp_git_repo, project_name="M6W1b")
    for bad in ("../x", "/etc", "a/b"):
        with pytest.raises(AwfApiError):
            _find_skill_file(bad, tmp_git_repo)


def test_w2_every_shipped_role_template_is_reachable_through_add_role(
    tmp_git_repo,
):
    """W2: a variant materializes the same file add_role must know."""
    api.init_project(tmp_git_repo, project_name="M6W2")
    templates = sorted(
        path
        for path in (TEMPLATES / "roles").glob("*.md")
        if path.stem != "supervisor"
    )
    assert templates
    for template in templates:
        result = api.add_role(tmp_git_repo, template.stem)
        body = _role(tmp_git_repo, template.stem).read_text(encoding="utf-8")
        assert result.role_file
        assert "Who you are" not in body, (
            f"{template.stem}: add_role записал заглушку вместо протокола"
        )
        assert "<describe the responsibility of this role>" not in body


def test_w3_variant_mapping_sets_config_updated(tmp_git_repo):
    """W3: an empty team + variant still reports the config write."""
    import yaml

    api.init_project(tmp_git_repo, project_name="M6W3")
    result = api.apply_project_setup(
        tmp_git_repo, team=[], flow_variant="full"
    )
    assert result.pipeline_file, result.as_dict()
    assert result.config_updated is True, result.as_dict()
    config = yaml.safe_load(
        (tmp_git_repo / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    )
    models = config.get("models") or {}
    assert (models.get("agent-implementer") or {}).get("agent_name") == "worker"


def test_w4_package_role_template_is_slug_fail_closed():
    """W4: the helper refuses a path-like name instead of resolving it."""
    from awf.api._templates import package_role_template

    assert package_role_template("../x") is None
    assert package_role_template("/etc/passwd") is None
    assert package_role_template("agent-implementer") is not None


def test_w5_single_variant_registry_feeds_cli_and_form(monkeypatch):
    """W5: a third registry entry appears in CLI choices and form data."""
    from agent_workflow_ui.tools import forms as forms_mod

    from awf import cli
    from awf.api import setup as setup_api

    registry = dict(setup_api.BUILTIN_PIPELINES)
    registry["third"] = "Третий вариант"
    monkeypatch.setattr(setup_api, "BUILTIN_PIPELINES", registry)
    monkeypatch.setattr(
        setup_api,
        "pipeline_variant_yaml_text",
        lambda variant, name=None: f"# {variant}",
    )
    monkeypatch.setattr(
        setup_api, "variant_unavailable_roles", lambda variant, project: []
    )

    parser, _ = cli._build_parser()
    write = next(
        action
        for action in parser._subparsers._group_actions[0].choices.values()
        if action.prog.endswith("pipeline-write")
    )
    variant_action = next(
        action for action in write._actions if "--variant" in action.option_strings
    )
    assert "third" in variant_action.choices, variant_action.choices

    preview = forms_mod._populate_flow_variant_previews(None)
    assert "third" in preview, preview
    labels = forms_mod._populate_flow_variant_labels(preview)
    assert labels["third"] == "Третий вариант"

    text = FORM.read_text(encoding="utf-8")
    assert "{% for variant in (flow_variants or {}) %}" in text
    assert "'full'" not in text and "code-health" not in text


# ─── X1/X2 (REVIEW виток 9): refresh обновляет managed-блоки; битый config ──


def test_x1_refresh_refreshes_an_existing_stale_discipline_block(tmp_git_repo):
    """X1: a stale managed block is replaced, not preserved as «already there»."""
    api.init_project(tmp_git_repo, project_name="M6StaleBlock")
    _write_skill(tmp_git_repo, "skill-stale", "# Stale\n\nBODY")
    api.add_role(tmp_git_repo, "stale", from_skill="skill-stale")

    path = _role(tmp_git_repo, "stale")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        DISCIPLINE_MARKER,
        DISCIPLINE_MARKER + "\nSTALE-DISCIPLINE-TEXT",
        1,
    )
    text = text.rstrip() + "\n\nUSER-NOTE: keep me\n"
    path.write_text(text, encoding="utf-8")

    result = api.add_role(tmp_git_repo, "stale", refresh=True)
    refreshed = path.read_text(encoding="utf-8")
    assert result.refresh_status == "updated", result.as_dict()
    assert "STALE-DISCIPLINE-TEXT" not in refreshed
    assert "таблицей «поверхность × исход»" in refreshed
    assert "USER-NOTE: keep me" in refreshed
    assert refreshed.count(DISCIPLINE_MARKER) == 1

    second = api.add_role(tmp_git_repo, "stale", refresh=True)
    assert second.refresh_status in ("unchanged", "skipped")
    assert path.read_text(encoding="utf-8") == refreshed


def test_x1_refresh_keeps_a_user_note_between_the_managed_blocks(tmp_git_repo):
    """X1: lines outside the marker spans survive in ANY position, not only EOF."""
    api.init_project(tmp_git_repo, project_name="M6Between")
    _write_skill(tmp_git_repo, "skill-between", "# Between\n\nBODY")
    api.add_role(tmp_git_repo, "between", from_skill="skill-between")

    path = _role(tmp_git_repo, "between")
    text = path.read_text(encoding="utf-8")
    needle = "---\n\n" + SUITE_MARKER
    assert needle in text
    path.write_text(
        text.replace(
            needle, "BETWEEN-BLOCKS-NOTE\n\n" + needle, 1
        ), encoding="utf-8"
    )

    result = api.add_role(tmp_git_repo, "between", refresh=True)
    refreshed = path.read_text(encoding="utf-8")
    assert result.refresh_status in ("updated", "unchanged"), result.as_dict()
    assert "BETWEEN-BLOCKS-NOTE" in refreshed, (
        "refresh съел пользовательскую строку между managed-блоками"
    )
    assert refreshed.count(DISCIPLINE_MARKER) == 1
    assert refreshed.count(SUITE_MARKER) == 1


def test_x2_config_get_marks_a_malformed_config(tmp_git_repo, capsys):
    """X2: broken YAML is rc=5, distinct from «key absent» (rc=1)."""
    from awf import cli

    api.init_project(tmp_git_repo, project_name="M6BrokenConfig")
    (tmp_git_repo / ".agentic" / "config.yaml").write_text(
        "project: [unclosed\n", encoding="utf-8"
    )

    rc = cli.main(
        ["config-get", "project.name", "--project-dir", str(tmp_git_repo)]
    )
    captured = capsys.readouterr()
    assert rc == 5, (rc, captured.out, captured.err)
    assert "malformed" in captured.err.lower(), captured.err
    assert captured.out == ""


def test_x2_suite_script_refuses_a_malformed_config(tmp_git_repo):
    """X2: the script says «broken config», not a green pytest fallback."""
    api.init_project(tmp_git_repo, project_name="M6BrokenSuite")
    script = tmp_git_repo / "scripts" / "suite-canonical.sh"
    (tmp_git_repo / ".agentic" / "config.yaml").write_text(
        "project: [unclosed\n", encoding="utf-8"
    )

    proc = _run_suite_script(tmp_git_repo, script)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert (
        "suite not configured: broken .agentic/config.yaml" in proc.stderr
    ), proc.stderr
    assert not list((tmp_git_repo / ".agentic" / "context").glob("SUITE-*.json"))


# ─── QA (эта стадия): закрытые разрывы покрытия A2/A3 ───────────────────


def test_x1_refresh_repairs_an_orphan_discipline_start_marker(tmp_git_repo):
    """A2: an orphan start marker is replaced, never doubled.

    X1 promises a broken managed block is replaced on refresh. Before the
    one-pattern fix, pair-removal could not see a start marker whose end
    marker was hand-deleted, so refresh appended a SECOND start marker
    (2 start / 1 end). The owner's note after the broken block must live.
    """
    api.init_project(tmp_git_repo, project_name="M6OrphanMarker")
    _write_skill(tmp_git_repo, "skill-orphan", "# Orphan\n\nBODY")
    api.add_role(tmp_git_repo, "orphan", from_skill="skill-orphan")

    path = _role(tmp_git_repo, "orphan")
    text = path.read_text(encoding="utf-8")
    assert DISCIPLINE_END in text
    text = text.replace(DISCIPLINE_END, "", 1)  # break the pair
    text = text.rstrip() + "\n\nUSER-NOTE: keep me\n"
    path.write_text(text, encoding="utf-8")

    result = api.add_role(tmp_git_repo, "orphan", refresh=True)

    refreshed = path.read_text(encoding="utf-8")
    assert result.refresh_status == "updated", result.as_dict()
    assert refreshed.count(DISCIPLINE_MARKER) == 1, (
        "refresh дописал второй start-маркер к осиротевшему блоку"
    )
    assert refreshed.count(DISCIPLINE_END) == 1
    assert "USER-NOTE: keep me" in refreshed, (
        "refresh съел заметку владельца после сломанного блока"
    )


def test_a3_variant_refuses_before_any_role_write_when_a_target_is_unsafe(
    tmp_git_repo, tmp_path
):
    """A3: every role target is preflighted before the FIRST write.

    A symlinked role leaf in the middle of the set used to leave the
    earlier roles materialized while the write path refused — a
    half-written variant. The refusal must precede every role file and
    the pipeline file.
    """
    api.init_project(tmp_git_repo, project_name="M6VarUnsafe")
    roles = tmp_git_repo / ".agentic" / "roles"
    outside = tmp_path / "outside-qa.md"
    (roles / "agent-qa-review.md").symlink_to(outside)

    with pytest.raises(api.AwfApiError):
        api.apply_project_setup(tmp_git_repo, team=[], flow_variant="full")

    assert not outside.exists(), "запись ушла за пределы проекта"
    partial = sorted(
        entry.name
        for entry in roles.iterdir()
        if entry.name not in ("supervisor.md", "agent-qa-review.md")
    )
    assert partial == [], f"частичная материализация ролей: {partial}"
    assert not (
        tmp_git_repo / ".agentic" / "pipelines" / "default.yaml"
    ).exists(), "пайплайн записан, хотя материализация ролей отказала"
