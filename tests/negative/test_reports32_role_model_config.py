"""REPORTS32 (TODO-0202): add_role пишет модель в config.yaml, статус — «офис».

Репорт «recovery i-commit-gate» п.6 + фича-репорт «дефолтный каталог ролей»
п.3–4: ``awf_add_role(from_skill=…, model=…)`` вписывал ``**Модель:**`` в
роле-файл, но движок модель оттуда НЕ читает (``get_role_model`` берёт
только ``.agentic/config.yaml``) — владелец вручную дописывал блоки в
config, иначе роли ушли бы на дефолт. «Легко обмануться, что роль уже
настроена».

Инварианты:
- ``add_role(..., model=…)`` мержит ``models.<slug>.model`` в
  ``.agentic/config.yaml`` (config — источник истины) — создаёт секцию
  ``models:`` при отсутствии, не трогает другие поля, сохраняет
  комментарии, идемпотентен; строка ``**Модель:**`` в роле-файле синхронна;
- ответ называет фактическое место модели (``model_config``);
- ``from_skill`` без model — поведение как раньше (config не трогает);
  template-режим тоже пишет config;
- draft-путь config НЕ трогает (ORCH M5.1);
- ``awf_status`` / ``awf_brief`` — «офис»: роли + модели + источник
  (config / только .md / нет) одним вызовом.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api.roles import model_line

# ─── fixtures / helpers ──────────────────────────────────────────────────


def _skill_env(tmp_git_repo: Path, monkeypatch) -> None:
    """Проект + глобальный скилл под приватным XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_git_repo / "xdg-home"))
    api.init_project(tmp_git_repo, project_name="RoleCfg")
    skill_dir = tmp_git_repo / "xdg-home" / "opencode" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 'A demo skill.'\n---\n\n"
        "# Demo Skill\n\nDo the demo thing.\n",
        encoding="utf-8",
    )


def _config_text(project: Path) -> str:
    return (project / ".agentic" / "config.yaml").read_text(encoding="utf-8")


def _config(project: Path) -> dict:
    return yaml.safe_load(_config_text(project))


# ─── (a) add_role(skill, model=X) → config + .md + response ─────────────


def test_from_skill_model_writes_config(tmp_git_repo, monkeypatch):
    """(a) from_skill + model → models.<slug>.model в config, .md строка,
    ответ называет место."""
    _skill_env(tmp_git_repo, monkeypatch)
    result = api.add_role(
        tmp_git_repo, "", from_skill="demo-skill", model="vllm/llm"
    )
    assert _config(tmp_git_repo)["models"]["demo-skill"]["model"] == "vllm/llm"
    content = (
        tmp_git_repo / ".agentic" / "roles" / "demo-skill.md"
    ).read_text(encoding="utf-8")
    assert model_line("vllm/llm") in content
    assert "Do the demo thing." in content
    assert result.model == "vllm/llm"
    assert result.model_config == "models.demo-skill.model"


def test_template_mode_writes_config(tmp_git_repo):
    """(a) template-режим (без from_skill) тоже пишет config."""
    api.init_project(tmp_git_repo, project_name="Tpl")
    result = api.add_role(tmp_git_repo, "qa", description="QA", model="vllm/llm")
    assert _config(tmp_git_repo)["models"]["qa"]["model"] == "vllm/llm"
    assert result.model_config == "models.qa.model"
    content = (tmp_git_repo / ".agentic" / "roles" / "qa.md").read_text(
        encoding="utf-8"
    )
    assert model_line("vllm/llm") in content


def test_built_in_mode_writes_config(tmp_git_repo):
    """built-in роль (agent-implementer) с model= пишет config."""
    api.init_project(tmp_git_repo, project_name="Builtin")
    result = api.add_role(tmp_git_repo, "agent-implementer", model="vllm/llm")
    assert _config(tmp_git_repo)["models"]["agent-implementer"]["model"] == (
        "vllm/llm"
    )
    assert result.model_config == "models.agent-implementer.model"


def test_special_char_model_round_trips(tmp_git_repo):
    """Модель с пробелом → YAML-скаляр в кавычках, safe_load даёт строку."""
    api.init_project(tmp_git_repo, project_name="Quote")
    api.add_role(tmp_git_repo, "qa", model="my model")
    assert _config(tmp_git_repo)["models"]["qa"]["model"] == "my model"


# ─── (b) идемпотентность ────────────────────────────────────────────────


def test_idempotent_second_call(tmp_git_repo):
    """(b) повторный вызов с той же моделью — config побайтово не меняется,
    дублей нет."""
    api.init_project(tmp_git_repo, project_name="Idem")
    api.add_role(tmp_git_repo, "qa", model="vllm/llm")
    cfg1 = _config_text(tmp_git_repo)
    result2 = api.add_role(tmp_git_repo, "qa", model="vllm/llm", force=True)
    cfg2 = _config_text(tmp_git_repo)
    assert cfg1 == cfg2  # побайтово — переписывания не было
    assert cfg2.count("model: vllm/llm") == 1  # ровно одна строка модели
    assert _config(tmp_git_repo)["models"]["qa"]["model"] == "vllm/llm"
    # место модели названо и на втором вызове (она уже в config)
    assert result2.model_config == "models.qa.model"


def test_repeated_model_no_duplicate_role_block(tmp_git_repo):
    """Повтор с force не дублирует блок роли."""
    api.init_project(tmp_git_repo, project_name="NoDup")
    api.add_role(tmp_git_repo, "qa", model="vllm/llm")
    api.add_role(tmp_git_repo, "qa", model="vllm/llm", force=True)
    text = _config_text(tmp_git_repo)
    assert text.count("  qa:") == 1


# ─── (c) config без секции models → создаётся, прочее цело ──────────────


def test_config_without_models_section_created(tmp_git_repo):
    """(c) нет секции models → создаётся, остальные ключи не тронуты."""
    api.init_project(tmp_git_repo, project_name="NoModels")
    cfg_path = tmp_git_repo / ".agentic" / "config.yaml"
    # уберём секцию models, оставив прочее содержимое (с комментарием)
    cfg_path.write_text(
        "project:\n  name: NoModels\n"
        "  root: \".\"\n"
        "verification:\n"
        "  test_cmd: 'pytest'\n"
        "  # комментарий, который должен выжить\n"
        "  lint_cmd: 'ruff'\n",
        encoding="utf-8",
    )
    result = api.add_role(tmp_git_repo, "qa", model="vllm/llm")
    cfg = _config(tmp_git_repo)
    assert cfg["models"]["qa"]["model"] == "vllm/llm"
    # прочее содержимое цело
    assert cfg["project"]["name"] == "NoModels"
    assert cfg["verification"]["test_cmd"] == "pytest"
    assert cfg["verification"]["lint_cmd"] == "ruff"
    assert result.model_config == "models.qa.model"


def test_comment_in_config_preserved(tmp_git_repo):
    """Комментарии в config.yaml выживают после мержа модели."""
    api.init_project(tmp_git_repo, project_name="Comment")
    cfg_path = tmp_git_repo / ".agentic" / "config.yaml"
    cfg_path.write_text(
        "models:\n"
        "  supervisor:\n"
        "    description: 'session'\n"
        "  # строка-комментарий про роли\n"
        "verification:\n"
        "  test_cmd: 'pytest'\n"
        "  # комментарий в verification\n"
        "  lint_cmd: 'ruff'\n",
        encoding="utf-8",
    )
    api.add_role(tmp_git_repo, "qa", model="vllm/llm")
    text = _config_text(tmp_git_repo)
    assert "# строка-комментарий про роли" in text
    assert "# комментарий в verification" in text
    assert _config(tmp_git_repo)["models"]["qa"]["model"] == "vllm/llm"
    assert _config(tmp_git_repo)["models"]["supervisor"]["description"] == (
        "session"
    )


def test_existing_role_keeps_sibling_keys(tmp_git_repo):
    """Мерж не стирает соседние ключи роли (agent_name и т.п.)."""
    api.init_project(tmp_git_repo, project_name="Sibling")
    cfg_path = tmp_git_repo / ".agentic" / "config.yaml"
    cfg_path.write_text(
        "models:\n"
        "  qa:\n"
        "    agent_name: worker\n"
        "verification:\n"
        "  test_cmd: 'pytest'\n",
        encoding="utf-8",
    )
    api.add_role(tmp_git_repo, "qa", model="vllm/llm", force=True)
    cfg = _config(tmp_git_repo)
    assert cfg["models"]["qa"]["model"] == "vllm/llm"
    assert cfg["models"]["qa"]["agent_name"] == "worker"


# ─── (d) статус показывает роли + модели + источник ─────────────────────


def _write_role(project: Path, slug: str, model_value: str | None) -> None:
    lines = [f"# ROLE: {slug}", ""]
    if model_value is not None:
        lines += [f"**Модель:** {model_value}", ""]
    lines.append("Body.")
    (project / ".agentic" / "roles" / f"{slug}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_status_office_shows_roles_models_source(tmp_git_repo):
    """(d) awf_status: офис — роли + модели + источник (config/md/none)."""
    api.init_project(tmp_git_repo, project_name="Office")
    # роль с моделью в config
    api.add_role(tmp_git_repo, "dev", model="vllm/llm")
    # роль с моделью ТОЛЬКО в .md (config не знает)
    _write_role(tmp_git_repo, "docs", "`md-only/model`")
    # роль без модели нигде
    _write_role(tmp_git_repo, "plain", None)

    status = api.get_status(tmp_git_repo)
    office = {e["role"]: e for e in status.office}
    assert office["dev"] == {
        "role": "dev", "model": "vllm/llm", "source": "config"
    }
    assert office["docs"] == {
        "role": "docs", "model": "md-only/model", "source": "md"
    }
    assert office["plain"] == {"role": "plain", "model": None, "source": "none"}


def test_status_office_config_wins_over_md(tmp_git_repo):
    """config — источник истины: при обеих моделях source=config."""
    api.init_project(tmp_git_repo, project_name="OfficeWin")
    api.add_role(tmp_git_repo, "dev", model="vllm/llm")
    # .md строка в расхождении (drift) — office всё равно от config
    _write_role(tmp_git_repo, "dev", "`other/model`")
    office = {e["role"]: e for e in api.get_status(tmp_git_repo).office}
    assert office["dev"] == {
        "role": "dev", "model": "vllm/llm", "source": "config"
    }


def test_brief_office_matches_status(tmp_git_repo):
    """awf_brief несёт тот же офис, что awf_status."""
    api.init_project(tmp_git_repo, project_name="BriefOffice")
    api.add_role(tmp_git_repo, "dev", model="vllm/llm")
    _write_role(tmp_git_repo, "plain", None)
    office = {e["role"]: e for e in api.brief(tmp_git_repo).office}
    assert office["dev"]["source"] == "config"
    assert office["dev"]["model"] == "vllm/llm"
    assert office["plain"]["source"] == "none"
    assert office["plain"]["model"] is None


def test_office_serializable_in_status_dict(tmp_git_repo):
    """office JSON-сериализуется в as_dict (MCP)."""
    import json

    api.init_project(tmp_git_repo, project_name="OfficeJson")
    api.add_role(tmp_git_repo, "dev", model="vllm/llm")
    d = api.get_status(tmp_git_repo).as_dict()
    assert "office" in d
    json.dumps(d["office"])


# ─── from_skill без model / draft / уже в config ────────────────────────


def test_from_skill_without_model_does_not_touch_config(tmp_git_repo, monkeypatch):
    """from_skill без model — config не трогает (поведение как раньше)."""
    _skill_env(tmp_git_repo, monkeypatch)
    before = _config_text(tmp_git_repo)
    result = api.add_role(tmp_git_repo, "", from_skill="demo-skill")
    after = _config_text(tmp_git_repo)
    assert before == after  # побайтово
    assert result.model_config == ""
    # модель не назначена — строка плейсхолдера
    content = (
        tmp_git_repo / ".agentic" / "roles" / "demo-skill.md"
    ).read_text(encoding="utf-8")
    assert "не назначена" in content


def test_model_resolved_from_config_not_rewritten(tmp_git_repo):
    """Модель уже в config, явного model= нет → переписывания нет."""
    api.init_project(tmp_git_repo, project_name="Already")
    cfg_path = tmp_git_repo / ".agentic" / "config.yaml"
    cfg_path.write_text(
        "models:\n  qa:\n    model: vllm/llm\n"
        "verification:\n  test_cmd: 'pytest'\n",
        encoding="utf-8",
    )
    before = _config_text(tmp_git_repo)
    result = api.add_role(tmp_git_repo, "qa")  # model= не задан
    after = _config_text(tmp_git_repo)
    assert before == after  # config не тронут
    # модель взята из config, но переписывать её не стали
    assert result.model == "vllm/llm"
    assert result.model_config == ""


def test_draft_path_does_not_touch_config(tmp_git_repo):
    """draft=True — config не трогает (ORCH M5.1), модель только в .md."""
    api.init_project(tmp_git_repo, project_name="Draft")
    before = _config_text(tmp_git_repo)
    result = api.add_role(
        tmp_git_repo, "qa", model="vllm/llm", draft=True
    )
    after = _config_text(tmp_git_repo)
    assert before == after
    assert result.model_config == ""
    assert result.model == "vllm/llm"
    # кандидат в draft/ со строкой модели
    candidate = (
        tmp_git_repo / ".agentic" / "roles" / "draft" / "qa.md"
    ).read_text(encoding="utf-8")
    assert model_line("vllm/llm") in candidate


# ─── отказ: битый YAML не поршаем ───────────────────────────────────────


def test_broken_config_refused_not_corrupted(tmp_git_repo):
    """Если мерж не может гарантировать валидный YAML — отказ, файл нетронут."""
    api.init_project(tmp_git_repo, project_name="Broken")
    cfg_path = tmp_git_repo / ".agentic" / "config.yaml"
    # models.<role> — скаляр, а не маппинг: добавить model: внутрь без
    # изменения структуры нельзя; валидатор должен поймать и отказаться.
    cfg_path.write_text(
        "models:\n  qa: just-a-string\nverification:\n  test_cmd: 'pytest'\n",
        encoding="utf-8",
    )
    before = _config_text(tmp_git_repo)
    with pytest.raises(api.AwfApiError):
        api.add_role(tmp_git_repo, "qa", model="vllm/llm")
    assert _config_text(tmp_git_repo) == before  # файл не тронут


# ─── CLI: подсказка «Add model config» заменена фактическим местом ──────


def test_cli_names_model_locus_not_manual_step(tmp_git_repo, capsys):
    """CLI печатает место модели, а не шаг «Add model config»."""
    import argparse

    from awf.cmd_add_role import run_create

    api.init_project(tmp_git_repo, project_name="Cli")
    args = argparse.Namespace(
        name="qa", description="", model="vllm/llm", from_skill="",
        force=False, draft=False, project_dir=str(tmp_git_repo),
    )
    rc = run_create(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Add model config" not in out  # старый ручной шаг убран
    assert "models.qa.model" in out  # фактическое место названо
    assert ".agentic/config.yaml" in out
