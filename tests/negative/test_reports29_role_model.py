"""REPORTS29 (TODO-0175): роль называет свою модель.

Решение владельца 09.10: «нам бы писать к ролям какая модель в роли
играет». Инварианты:

- add_role кладёт единообразную строку модели в файл роли для ВСЕХ
  источников (built-in шаблон, placeholder, from_skill): явный
  ``model=`` → ``models:`` конфига по имени роли → «не назначена
  (действует модель конфига по умолчанию)»;
- тело скилла при from_skill не искажается: front-matter вырезан,
  провенанс на первой строке, строка модели — рядом с ним;
- ``check_model_config`` сверяет строки моделей ролей
  ``.agentic/roles/*.md`` с ``models:`` конфига: расхождение → warning
  со списком «роль: файл=…, конфиг=…»; согласованные → тишина;
  отсутствующая строка — не ошибка;
- движок не меняется: конфиг остаётся источником истины (здесь
  проверяем только, что сверка отчитывается, а не переопределяет).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api.model_check import check_model_config
from awf.api.roles import (
    UNASSIGNED_MODEL_TEXT,
    model_line,
    parse_role_model_line,
)

# ─── add_role: строка модели по источникам ──────────────────────────────


def _set_models(project: Path, models: dict) -> None:
    config_path = project / ".agentic" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["models"] = models
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")


def test_placeholder_explicit_model_line(tmp_git_repo):
    """Явный model= → строка с ним, result.model = он."""
    (tmp_git_repo / ".agentic").mkdir()
    result = api.add_role(tmp_git_repo, "qa", description="QA", model="vllm/llm")
    content = (tmp_git_repo / ".agentic" / "roles" / "qa.md").read_text(encoding="utf-8")
    assert model_line("vllm/llm") in content
    assert result.model == "vllm/llm"


def test_placeholder_model_from_config(tmp_git_repo):
    """Без явного model= → берётся models.<роль>.model из конфига."""
    api.init_project(tmp_git_repo, project_name="RoleModelCfg")
    _set_models(tmp_git_repo, {
        "agent-x": {"agent_name": "worker", "model": "vllm/llm"},
    })
    result = api.add_role(tmp_git_repo, "agent-x")
    content = (tmp_git_repo / ".agentic" / "roles" / "agent-x.md").read_text(
        encoding="utf-8"
    )
    assert model_line("vllm/llm") in content
    assert result.model == "vllm/llm"


def test_placeholder_unassigned_when_nothing(tmp_git_repo):
    """Нигде нет модели → строка с плейсхолдером «не назначена»."""
    (tmp_git_repo / ".agentic").mkdir()
    result = api.add_role(tmp_git_repo, "plain")
    content = (tmp_git_repo / ".agentic" / "roles" / "plain.md").read_text(
        encoding="utf-8"
    )
    assert model_line("") in content
    assert UNASSIGNED_MODEL_TEXT in content
    assert result.model == ""


def test_builtin_carries_model_line_body_intact(tmp_git_repo):
    """Built-in шаблон: строка модели добавлена, протокол нетронут."""
    api.init_project(tmp_git_repo, project_name="RoleModel")
    _set_models(tmp_git_repo, {
        "agent-implementer": {"agent_name": "worker", "model": "vllm/llm"},
    })
    result = api.add_role(tmp_git_repo, "agent-implementer")
    content = (
        tmp_git_repo / ".agentic" / "roles" / "agent-implementer.md"
    ).read_text(encoding="utf-8")
    assert model_line("vllm/llm") in content
    assert result.model == "vllm/llm"
    # тело встроенного шаблона цело
    for marker in ("# ROLE: agent-implementer", "контракт TODO", "Self-check"):
        assert marker in content, f"шаблон потерял маркер: {marker}"


def _skill_env(tmp_git_repo, monkeypatch):
    """Проект + глобальный скилл под приватным XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_git_repo / "xdg-home"))
    api.init_project(tmp_git_repo, project_name="FromSkill")
    skill_dir = tmp_git_repo / "xdg-home" / "opencode" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: 'A demo skill.'\n---\n\n"
        "# Demo Skill\n\nDo the demo thing.\n",
        encoding="utf-8",
    )
    return skill_file


def test_from_skill_carries_model_line(tmp_git_repo, monkeypatch):
    """from_skill: строка модели рядом с провенансом, тело не искажено."""
    skill_file = _skill_env(tmp_git_repo, monkeypatch)
    result = api.add_role(
        tmp_git_repo, "demo-skill", from_skill="demo-skill", model="vllm/llm"
    )
    content = (
        tmp_git_repo / ".agentic" / "roles" / "demo-skill.md"
    ).read_text(encoding="utf-8")
    lines = content.split("\n")
    # провенанс — первая строка, строка модели — рядом (вторая)
    assert lines[0].startswith("<!-- copied from skill `demo-skill`")
    assert lines[1] == model_line("vllm/llm")
    # тело скилла цело, свой front-matter вырезан
    assert "Do the demo thing." in content
    assert "name: demo-skill" not in content
    assert result.model == "vllm/llm"


def test_from_skill_model_from_config(tmp_git_repo, monkeypatch):
    """from_skill без явного model= → строка из конфига по имени роли."""
    skill_file = _skill_env(tmp_git_repo, monkeypatch)
    _set_models(tmp_git_repo, {
        "demo-skill": {"agent_name": "worker", "model": "vllm/llm"},
    })
    result = api.add_role(tmp_git_repo, "", from_skill="demo-skill")
    content = (
        tmp_git_repo / ".agentic" / "roles" / "demo-skill.md"
    ).read_text(encoding="utf-8")
    assert model_line("vllm/llm") in content
    assert result.model == "vllm/llm"
    assert "Do the demo thing." in content


# ─── парсер строки модели ────────────────────────────────────────────────


def test_parse_line_formats():
    """Парсер: с бэктиками, без (legacy), плейсхолдер, отсутствие."""
    assert parse_role_model_line(model_line("vllm/llm")) == "vllm/llm"
    # legacy без бэктиков (старые placeholder-файлы) тоже читается
    assert parse_role_model_line("**Model:** vllm/llm") == "vllm/llm"
    assert parse_role_model_line(model_line("")) is None
    assert parse_role_model_line("# ROLE: qa\nno line here\n") is None


# ─── check_model_config: сверка с ролями ─────────────────────────────────


@pytest.fixture
def mock_oc_env(monkeypatch, tmp_path):
    """Изолированная среда opencode: CLI пуст, opencode.json — фейковый.

    Перехватывается только 'opencode models' — остальные вызовы
    (git и т.п.) идут в реальный subprocess.run.
    """

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["opencode"] and "models" in cmd:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)
    oc_dir = tmp_path / "oc"
    oc_dir.mkdir()
    (oc_dir / "opencode.json").write_text(json.dumps({
        "provider": {"vllm": {"models": {"llm": {"name": "LLM"}}}},
    }))
    import awf.xdg as xdg_mod

    monkeypatch.setattr(xdg_mod, "opencode_config_file", lambda: oc_dir / "opencode.json")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-db"))


def _write_role(project: Path, slug: str, model_value: str | None) -> None:
    lines = [f"# ROLE: {slug}", ""]
    if model_value is not None:
        lines += [f"**Модель:** {model_value}", ""]
    lines.append("Body line.")
    (project / ".agentic" / "roles" / f"{slug}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_check_warns_on_role_file_drift(tmp_git_repo, mock_oc_env):
    """Роль с чужой моделью в файле → warning «роль: файл=…, конфиг=…»."""
    api.init_project(tmp_git_repo, project_name="RoleModelCheck")
    _set_models(tmp_git_repo, {
        "agent-x": {"agent_name": "worker", "model": "vllm/llm"},
    })
    _write_role(tmp_git_repo, "agent-x", "`other/model`")

    result = check_model_config(tmp_git_repo)

    drift = [w for w in result["warnings"] if "agent-x" in w]
    assert len(drift) == 1
    assert "other/model" in drift[0]
    assert "vllm/llm" in drift[0]
    # сверка видна в result
    rf = {e["role"]: e for e in result["role_files"]}
    assert rf["agent-x"]["file_model"] == "other/model"
    assert rf["agent-x"]["config_model"] == "vllm/llm"
    assert rf["agent-x"]["match"] is False


def test_check_quiet_when_consistent(tmp_git_repo, mock_oc_env):
    """Согласованные строки → ни одного warning, match=True."""
    api.init_project(tmp_git_repo, project_name="RoleModelCheck")
    _set_models(tmp_git_repo, {
        "agent-x": {"agent_name": "worker", "model": "vllm/llm"},
    })
    _write_role(tmp_git_repo, "agent-x", "`vllm/llm`")

    result = check_model_config(tmp_git_repo)

    assert result["warnings"] == []
    rf = {e["role"]: e for e in result["role_files"]}
    assert rf["agent-x"]["match"] is True


def test_check_missing_line_is_not_error(tmp_git_repo, mock_oc_env):
    """Роль без строки модели → не ошибка, warning не появляется."""
    api.init_project(tmp_git_repo, project_name="RoleModelCheck")
    _set_models(tmp_git_repo, {
        "agent-x": {"agent_name": "worker", "model": "vllm/llm"},
    })
    _write_role(tmp_git_repo, "agent-x", None)

    result = check_model_config(tmp_git_repo)

    assert not any("agent-x" in w for w in result["warnings"])
    rf = {e["role"]: e for e in result["role_files"]}
    assert rf["agent-x"]["file_model"] is None
    assert rf["agent-x"]["match"] is True  # нечего сверять — не расхождение


def test_check_unassigned_line_is_quiet(tmp_git_repo, mock_oc_env):
    """Строка «не назначена» → конфигурационная по умолчанию, тишина."""
    api.init_project(tmp_git_repo, project_name="RoleModelCheck")
    _set_models(tmp_git_repo, {
        "agent-x": {"agent_name": "worker", "model": "vllm/llm"},
    })
    _write_role(tmp_git_repo, "agent-x", UNASSIGNED_MODEL_TEXT)

    result = check_model_config(tmp_git_repo)

    assert not any("agent-x" in w for w in result["warnings"])
    rf = {e["role"]: e for e in result["role_files"]}
    assert rf["agent-x"]["file_model"] is None


def test_check_result_serializable(tmp_git_repo, mock_oc_env):
    """result с role_files JSON-сериализуется (MCP)."""
    api.init_project(tmp_git_repo, project_name="RoleModelCheck")
    _write_role(tmp_git_repo, "agent-x", "`vllm/llm`")
    result = check_model_config(tmp_git_repo)
    json.dumps(result)
