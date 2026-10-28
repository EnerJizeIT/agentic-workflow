"""REPORTS33 (TODO-0209): project-setup-форма — состав стадий как осознанный
выбор + превью pipeline.yaml.

Source: awf-feature-20261012-menshe-ruchnoy-tseremonii-roli-stadii-prodolzhenie-proekta.md
(1.8.0), пункт (2): форма из выбранной команды сгенерировала ЛИНЕЙНЫЙ
пайплайн «все члены подряд» (implementer → qa-review → project-auditor) —
каждый TODO проходил бы через всех; владелец обошёл именованными
пайплайнами bugfix/audit с пинами.

Invariants:
(a) apply с составом «одна роль» → pipeline.yaml: plan → рабочая роль →
    verify, без аудиторов в цепочке; QA-стадия — только при include_qa
    и при наличии QA-роли в команде (и если она не основная);
(b) состав «цепочка» — прежний линейный состав; отправка без stage_mode
    (старая форма) — тоже цепочка (back-compat);
(c) превью-данные (уровень render/данных) содержат текст итогового
    pipeline — побайто идентичен тому, что apply записал в
    .agentic/pipelines/;
(d) повторный apply с теми же данными идемпотентен: файл не меняется,
    состав не дублируется.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf import api

# Команда из репорта: рабочая роль + QA + аудитор. На «цепочке» каждый
# юнит проходит через всех троих — именно это и было сюрпризом.
TEAM = [
    {"type": "default", "agent": "agent-implementer", "model": ""},
    {"type": "default", "agent": "agent-qa-review", "model": ""},
    {"type": "default", "agent": "agent-project-auditor", "model": ""},
]


@pytest.fixture
def awf_project(tmp_git_repo):
    """Проект с инициализированным .agentic/ (config.yaml + supervisor.md)."""
    api.init_project(tmp_git_repo, project_name="SetupForm")
    return tmp_git_repo


def _pipeline_text(proj: Path) -> str:
    return (proj / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8")


def _roles(proj: Path) -> list[str]:
    stages = yaml.safe_load(_pipeline_text(proj))["stages"]
    return [s["role"] for s in stages]


# ─── (a) «одна роль»: plan → рабочая роль → verify ─────────────────────────


class TestSingleRoleMode:
    def test_single_role_without_qa(self, awf_project):
        result = api.apply_project_setup(
            awf_project,
            team=TEAM,
            stage_mode="single",
            primary_role="agent-implementer",
            include_qa=False,
        )
        assert result.pipeline_file is not None
        # plan → implementer → closer → verify: ни аудитора, ни QA в цепочке
        assert _roles(awf_project) == [
            "supervisor", "agent-implementer", "agent-implementer", "supervisor",
        ]
        assert "agent-project-auditor" not in _roles(awf_project)

    def test_single_role_with_qa(self, awf_project):
        api.apply_project_setup(
            awf_project,
            team=TEAM,
            stage_mode="single",
            primary_role="agent-implementer",
            include_qa=True,
        )
        assert _roles(awf_project) == [
            "supervisor",
            "agent-implementer",
            "agent-qa-review",
            "agent-implementer",
            "supervisor",
        ]
        assert "agent-project-auditor" not in _roles(awf_project)

    def test_primary_defaults_to_first_team_role(self, awf_project):
        api.apply_project_setup(awf_project, team=TEAM, stage_mode="single")
        assert _roles(awf_project) == [
            "supervisor", "agent-implementer", "agent-implementer", "supervisor",
        ]

    def test_primary_not_in_team_falls_back_with_warning(self, awf_project):
        result = api.apply_project_setup(
            awf_project,
            team=TEAM,
            stage_mode="single",
            primary_role="ghost-role",
        )
        # деградация, не сбой: основная = первая роль команды
        assert _roles(awf_project) == [
            "supervisor", "agent-implementer", "agent-implementer", "supervisor",
        ]
        assert any("ghost-role" in w for w in result.warnings)

    def test_include_qa_without_qa_role_warns_and_skips(self, awf_project):
        result = api.apply_project_setup(
            awf_project,
            team=[{"type": "default", "agent": "agent-implementer"}],
            stage_mode="single",
            include_qa=True,
        )
        assert _roles(awf_project) == [
            "supervisor", "agent-implementer", "agent-implementer", "supervisor",
        ]
        assert any("QA" in w for w in result.warnings)

    def test_single_keeps_closer_gate(self, awf_project):
        """IMP7 (TODO-0290): single is not "one agent" — the closer runs."""
        api.apply_project_setup(awf_project, team=TEAM, stage_mode="single")
        stages = yaml.safe_load(_pipeline_text(awf_project))["stages"]
        assert any(
            s.get("id") == "implementer-close" for s in stages
        ), stages
        assert stages[-1]["name"] == "verify"

    def test_primary_is_qa_role_no_duplicate_stage(self, awf_project):
        api.apply_project_setup(
            awf_project,
            team=TEAM,
            stage_mode="single",
            primary_role="agent-qa-review",
            include_qa=True,
        )
        roles = _roles(awf_project)
        assert roles.count("agent-qa-review") == 1
        assert roles == [
            "supervisor", "agent-qa-review", "agent-implementer", "supervisor",
        ]


# ─── (b) «цепочка» — прежний линейный состав ───────────────────────────────


class TestChainMode:
    def test_chain_keeps_linear_composition(self, awf_project):
        api.apply_project_setup(awf_project, team=TEAM, stage_mode="chain")
        assert _roles(awf_project) == [
            "supervisor",
            "agent-implementer",
            "agent-qa-review",
            "agent-implementer",
            "agent-project-auditor",
            "supervisor",
        ]

    def test_omitted_stage_mode_stays_chain(self, awf_project):
        """Back-compat: отправка старой формы без поля stage_mode."""
        api.apply_project_setup(awf_project, team=TEAM)
        assert _roles(awf_project) == [
            "supervisor",
            "agent-implementer",
            "agent-qa-review",
            "agent-implementer",
            "agent-project-auditor",
            "supervisor",
        ]

    def test_unknown_stage_mode_falls_back_to_chain(self, awf_project):
        result = api.apply_project_setup(
            awf_project, team=TEAM, stage_mode="diagonal"
        )
        assert len(_roles(awf_project)) == 6
        assert any("diagonal" in w for w in result.warnings)


# ─── (c) превью: данные содержат текст итогового pipeline ──────────────────


class TestPreview:
    def test_preview_text_identical_to_written_file(self, awf_project):
        """Превью и записанный файл — один билдер, побайто одинаковый текст."""
        expected = api.setup.pipeline_yaml_text(
            TEAM,
            name="default",
            stage_mode="single",
            primary_role="agent-implementer",
        )
        api.apply_project_setup(
            awf_project,
            team=TEAM,
            stage_mode="single",
            primary_role="agent-implementer",
        )
        assert _pipeline_text(awf_project) == expected

    def test_form_text_names_the_closer_gate(self, awf_project):
        """TODO-0290: the single label/hint say closer, not "one agent"."""
        from agent_workflow_ui.render.engine import (
            create_default_env,
            render_template,
        )

        single_text = api.setup.pipeline_yaml_text(
            TEAM, name="default", stage_mode="single",
        )
        rendered = render_template(create_default_env(), "project-setup", {
            "form_id": "FORM-test",
            "submit_url": "http://127.0.0.1:1/submit/FORM-test",
            "available_roles": [],
            "pipeline_preview": {"name": "default", "single": single_text},
        })
        assert "closer-гейт" in rendered
        assert "Одна рабочая роль на юнит (+ verify)" not in rendered

    def test_render_data_contains_pipeline_text(self, awf_project):
        """Уровень render: в контекст формы попадает текст pipeline.yaml."""
        from agent_workflow_ui.render.engine import (
            create_default_env,
            render_template,
        )

        single_text = api.setup.pipeline_yaml_text(
            TEAM, name="default", stage_mode="single",
            primary_role="agent-implementer",
        )
        rendered = render_template(create_default_env(), "project-setup", {
            "form_id": "FORM-test",
            "submit_url": "http://127.0.0.1:1/submit/FORM-test",
            "available_roles": [],
            "pipeline_preview": {
                "name": "default",
                "chain": api.setup.pipeline_yaml_text(
                    TEAM, name="default", stage_mode="chain"
                ),
                "single": single_text,
            },
        })
        # дефолтный состав («одна роль») показан в превью как есть; the
        # <pre> is autoescaped by Jinja, so compare its own escaping.
        from markupsafe import escape as _escape

        assert str(_escape(single_text)) in rendered
        # и поля выбора состава присутствуют в форме
        assert 'name="stage_mode"' in rendered
        assert 'name="primary_role"' in rendered
        assert 'name="include_qa"' in rendered

    def test_open_form_preview_data(self, awf_project):
        """Уровень данных open_form: оба состава предрассчитаны из команды."""
        from agent_workflow_ui.tools.forms import _populate_project_setup_preview

        preview = _populate_project_setup_preview({"team_config": TEAM}, awf_project)

        assert preview["name"] == "default"
        assert preview["single"] == api.setup.pipeline_yaml_text(
            TEAM, name="default", stage_mode="single"
        )
        assert preview["chain"] == api.setup.pipeline_yaml_text(
            TEAM, name="default", stage_mode="chain"
        )
        # «одна роль» без QA: в цепочке только рабочая роль
        assert "agent-project-auditor" not in preview["single"]
        # «цепочка» — все роли
        assert "agent-project-auditor" in preview["chain"]

    def test_open_form_preview_without_team_is_empty(self, awf_project):
        """Без команды в данных превью пустое — JS достраивает из строк формы."""
        from agent_workflow_ui.tools.forms import _populate_project_setup_preview

        assert _populate_project_setup_preview({}, awf_project) == {
            "name": "", "chain": "", "single": "",
        }


# ─── (d) повторный apply идемпотентен ───────────────────────────────────────


class TestIdempotentResubmit:
    def test_resubmit_same_data_same_pipeline(self, awf_project):
        kwargs = dict(
            team=TEAM, stage_mode="single",
            primary_role="agent-implementer", include_qa=True,
        )
        api.apply_project_setup(awf_project, **kwargs)
        text1 = _pipeline_text(awf_project)
        config1 = (awf_project / ".agentic" / "config.yaml").read_text(encoding="utf-8")

        api.apply_project_setup(awf_project, **kwargs)
        text2 = _pipeline_text(awf_project)
        config2 = (awf_project / ".agentic" / "config.yaml").read_text(encoding="utf-8")

        assert text2 == text1
        assert config2 == config1
        # состав не дублируется повторной отправкой
        assert _roles(awf_project) == [
            "supervisor",
            "agent-implementer",
            "agent-qa-review",
            "agent-implementer",
            "supervisor",
        ]

    def test_resubmit_mode_switch_replaces_pipeline(self, awf_project):
        """Повторная отправка с другим составом — прежний состав не остаётся."""
        api.apply_project_setup(
            awf_project, team=TEAM, stage_mode="chain"
        )
        api.apply_project_setup(
            awf_project,
            team=TEAM, stage_mode="single",
            primary_role="agent-implementer",
        )
        assert _roles(awf_project) == [
            "supervisor", "agent-implementer", "agent-implementer", "supervisor",
        ]


# ─── plugin-обвязка: поля формы доходят до apply ───────────────────────────


@pytest.fixture
def isolated_roles_dir(tmp_path, monkeypatch):
    """GLOBAL_ROLES_DIR → tmp_path/roles (как в test_roles_processor)."""
    from agent_workflow_ui import opencode_config, state

    roles = tmp_path / "roles"
    roles.mkdir(parents=True)
    monkeypatch.setattr(opencode_config, "GLOBAL_ROLES_DIR", roles)
    original = state._project_dir
    state._project_dir = None
    yield roles
    state._project_dir = original


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    (proj / ".agentic" / "roles").mkdir(parents=True)
    return proj


class TestFormSubmitPlumbing:
    def test_process_role_saves_passes_stage_mode(self, isolated_roles_dir, tmp_path):
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = _make_project(tmp_path)
        data = {
            "team_config": json.dumps(TEAM),
            "stage_mode": "single",
            "primary_role": "agent-implementer",
            "include_qa": "on",
        }
        process_role_saves(data, project_dir=proj)

        target = proj / ".agentic" / "pipelines" / "default.yaml"
        roles = [s["role"] for s in yaml.safe_load(target.read_text())["stages"]]
        assert roles == [
            "supervisor",
            "agent-implementer",
            "agent-qa-review",
            "agent-implementer",
            "supervisor",
        ]

    def test_process_role_saves_legacy_submit_stays_chain(
        self, isolated_roles_dir, tmp_path
    ):
        """Отправка без новых полей (старый клиент) — прежняя цепочка."""
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = _make_project(tmp_path)
        process_role_saves(
            {"team_config": json.dumps(TEAM)}, project_dir=proj
        )
        target = proj / ".agentic" / "pipelines" / "default.yaml"
        roles = [s["role"] for s in yaml.safe_load(target.read_text())["stages"]]
        assert len(roles) == 6
        assert roles[-1] == "supervisor"
