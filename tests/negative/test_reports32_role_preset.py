"""REPORTS32 (TODO-0203): пресет-каталог ролей — «типовой офис» одним вызовом.

Фича-репорт «дефолтный каталог ролей» п.1–2 (jql, 1.7.0): типовые роли
(system-analyst, spec-writer, architector, project-auditor, qa-review,
qa-table, auditor-table, product) в каждом проекте собирались руками по
одной из скиллов + ручное сопоставление ролей с моделями в config.yaml.
Нужен «рабочий офис» одним вызовом.

Инварианты:
- каталог пресета «office» вшит в ``awf/api/roles.py``: роль → скилл
  ``agent-*``; ``product`` — только если скилл найден на машине;
- ``seed_roles(preset="office")`` (MCP — ``awf_add_role(preset="office")``)
  создаёт только отсутствующие роли; существующие НЕ трогает (skip с
  пометкой); отсутствующий скилл — не ошибка, а строка в результате;
- опциональный ``models={role: model}`` пишет ``models.<role>.model`` в
  config.yaml механизмом TODO-0202 (хирургический мерж, идемпотентен) —
  в том числе для ролей, которые уже существуют;
- ответ — таблица: created / skipped / models (что и куда, action
  written/unchanged);
- повторный вызов — no-op: роле-файлы и config побайтово не меняются.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from awf import api

# AUD12-13 / test infra: mcp evaluates a `subprocess.Popen[bytes]`
# annotation at class-creation — import at COLLECTION time, not inside a
# test body (the autouse conftest fixture replaces subprocess.Popen).
try:
    import mcp.server.fastmcp  # noqa: F401
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False

from agent_workflow_ui.tools import awf as awf_mod  # noqa: E402

# ─── fixtures / helpers ──────────────────────────────────────────────────

# Каталог «office»: роль → скилл agent-* (фиксирован фича-репортом).
# agent-product на машине НЕ создаётся: product — только если скилл есть.
OFFICE_SKILLS = (
    "agent-system-analyst",
    "agent-spec-writer",
    "agent-architector",
    "agent-project-auditor",
    "agent-qa-review",
    "agent-qa-table",
    "agent-auditor-table",
)
OFFICE_ROLES = (
    "system-analyst",
    "spec-writer",
    "architector",
    "project-auditor",
    "qa-review",
    "qa-table",
    "auditor-table",
)


def _office_env(tmp_git_repo: Path, monkeypatch, skills=OFFICE_SKILLS) -> None:
    """Проект + глобальные скиллы под приватным XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_git_repo / "xdg-home"))
    api.init_project(tmp_git_repo, project_name="OfficePreset")
    for skill in skills:
        skill_dir = tmp_git_repo / "xdg-home" / "opencode" / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: '{skill} demo.'\n---\n\n"
            f"# {skill}\n\nBody of {skill}.\n",
            encoding="utf-8",
        )


def _config_text(project: Path) -> str:
    return (project / ".agentic" / "config.yaml").read_text(encoding="utf-8")


def _config(project: Path) -> dict:
    return yaml.safe_load(_config_text(project))


def _run(coro):
    return asyncio.run(coro)


# ─── (a) пустой проект → полный набор, ответ-таблица ────────────────────


def test_preset_creates_office(tmp_git_repo, monkeypatch):
    """(a) tmp-проект без ролей → preset создаёт набор; ответ — таблица."""
    _office_env(tmp_git_repo, monkeypatch)
    result = api.seed_roles(tmp_git_repo)
    assert result.preset == "office"
    assert [row["role"] for row in result.created] == list(OFFICE_ROLES)
    for role in OFFICE_ROLES:
        f = tmp_git_repo / ".agentic" / "roles" / f"{role}.md"
        assert f.is_file(), role
        content = f.read_text(encoding="utf-8")
        # skill-источник: provenance + тело, front-matter скилла снят
        assert f"copied from skill `agent-{role}`" in content
        assert f"Body of agent-{role}." in content
        assert not content.lstrip().startswith("---")
    # product: скилл на машине нет → строка в skipped, не падение
    skipped = {row["role"]: row["reason"] for row in result.skipped}
    assert set(skipped) == {"product"}
    assert "agent-product" in skipped["product"]
    # без models= таблица моделей пуста
    assert result.models == []


def test_preset_models_write_config(tmp_git_repo, monkeypatch):
    """(a) models= → config.yaml получает models.<role>.model, строка
    в .md синхронна, ответ называет что и куда."""
    _office_env(tmp_git_repo, monkeypatch)
    models = {
        "system-analyst": "vllm/llm",
        "qa-review": "opencode-go/glm-5.3-flash",
    }
    result = api.seed_roles(tmp_git_repo, models=models)
    cfg = _config(tmp_git_repo)
    assert cfg["models"]["system-analyst"]["model"] == "vllm/llm"
    assert cfg["models"]["qa-review"]["model"] == "opencode-go/glm-5.3-flash"
    content = (
        tmp_git_repo / ".agentic" / "roles" / "system-analyst.md"
    ).read_text(encoding="utf-8")
    assert "**Модель:** `vllm/llm`" in content
    rows = {row["role"]: row for row in result.models}
    assert rows["system-analyst"]["locus"] == "models.system-analyst.model"
    assert rows["system-analyst"]["action"] == "written"
    assert rows["qa-review"]["model"] == "opencode-go/glm-5.3-flash"
    created = {row["role"]: row for row in result.created}
    assert created["system-analyst"]["model"] == "vllm/llm"


# ─── (b) повторный вызов — no-op ─────────────────────────────────────────


def test_second_call_noop(tmp_git_repo, monkeypatch):
    """(b) повтор → всё skipped, роле-файлы и config побайтово не меняются
    (mtime тоже)."""
    _office_env(tmp_git_repo, monkeypatch)
    models = {"system-analyst": "vllm/llm"}
    api.seed_roles(tmp_git_repo, models=models)
    roles_dir = tmp_git_repo / ".agentic" / "roles"
    before = {
        f.name: (f.read_bytes(), f.stat().st_mtime_ns)
        for f in roles_dir.glob("*.md")
    }
    cfg_before = _config_text(tmp_git_repo)

    result = api.seed_roles(tmp_git_repo, models=models)

    assert result.created == []
    skipped = {row["role"] for row in result.skipped}
    assert skipped == set(OFFICE_ROLES) | {"product"}
    after = {
        f.name: (f.read_bytes(), f.stat().st_mtime_ns)
        for f in roles_dir.glob("*.md")
    }
    assert before == after  # побайтово + mtime
    assert _config_text(tmp_git_repo) == cfg_before  # побайтово
    rows = {row["role"]: row for row in result.models}
    assert rows["system-analyst"]["action"] == "unchanged"


# ─── (c) существующая кастомная роль сохранена ───────────────────────────


def test_existing_custom_role_preserved(tmp_git_repo, monkeypatch):
    """(c) кастомная роль с каталожным slug — побайтово сохранена;
    чужая роль (не из каталога) в таблицах не фигурирует."""
    _office_env(tmp_git_repo, monkeypatch)
    roles_dir = tmp_git_repo / ".agentic" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    custom = roles_dir / "system-analyst.md"
    custom.write_text(
        "# My hand-written analyst\n\nCareful body.\n", encoding="utf-8"
    )
    unrelated = roles_dir / "my-own-auditor.md"
    unrelated.write_text("# Mine\n", encoding="utf-8")

    result = api.seed_roles(tmp_git_repo)

    assert custom.read_text(encoding="utf-8") == (
        "# My hand-written analyst\n\nCareful body.\n"
    )
    skipped = {row["role"]: row["reason"] for row in result.skipped}
    assert "system-analyst" in skipped
    assert "skill not found" not in skipped["system-analyst"]
    assert "my-own-auditor" not in skipped
    created = {row["role"] for row in result.created}
    assert "system-analyst" not in created
    assert unrelated.read_text(encoding="utf-8") == "# Mine\n"


# ─── (d) скилл отсутствует → строка «пропущен», не падение ──────────────


def test_missing_skill_is_a_row_not_an_error(tmp_git_repo, monkeypatch):
    """(d) половина скиллов отсутствует → только найденные созданы,
    остальные — строки skipped с причиной."""
    _office_env(
        tmp_git_repo, monkeypatch,
        skills=("agent-qa-table", "agent-system-analyst"),
    )
    result = api.seed_roles(tmp_git_repo)
    created = {row["role"] for row in result.created}
    assert created == {"qa-table", "system-analyst"}
    skipped = {row["role"]: row["reason"] for row in result.skipped}
    # 5 недостающих скиллов из каталога + product (скилла нет на машине)
    assert len(skipped) == 6
    for role in skipped:
        assert role not in created
        assert "skill not found" in skipped[role]


# ─── models= на существующие роли / отказы ───────────────────────────────


def test_models_apply_to_existing_roles(tmp_git_repo, monkeypatch):
    """models= для уже существующей роли → config обновлён, роле-файл
    побайтово не тронут."""
    _office_env(tmp_git_repo, monkeypatch)
    api.seed_roles(tmp_git_repo)
    role_file = tmp_git_repo / ".agentic" / "roles" / "qa-review.md"
    role_before = role_file.read_bytes()
    cfg_before = _config_text(tmp_git_repo)

    result = api.seed_roles(tmp_git_repo, models={"qa-review": "deepseek-v4.1-flash"})

    assert _config(tmp_git_repo)["models"]["qa-review"]["model"] == (
        "deepseek-v4.1-flash"
    )
    assert role_file.read_bytes() == role_before  # файл не тронут
    assert cfg_before != _config_text(tmp_git_repo)
    assert result.created == []
    rows = {row["role"]: row for row in result.models}
    assert rows["qa-review"]["action"] == "written"


def test_unknown_preset_refused(tmp_git_repo):
    """Неизвестный пресет — typed-отказ со списком доступных."""
    api.init_project(tmp_git_repo, project_name="PresetX")
    with pytest.raises(api.AwfApiError, match="office"):
        api.seed_roles(tmp_git_repo, preset="bogus")


def test_models_unknown_key_refused(tmp_git_repo, monkeypatch):
    """models= с ключом вне каталога пресета — отказ, ничего не создано."""
    _office_env(tmp_git_repo, monkeypatch)
    with pytest.raises(api.AwfApiError, match="qa_review"):
        api.seed_roles(tmp_git_repo, models={"qa_review": "vllm/llm"})
    roles_dir = tmp_git_repo / ".agentic" / "roles"
    # каталожных ролей не создано (supervisor.md от init не в счёт)
    assert not any(
        (roles_dir / f"{role}.md").is_file() for role in OFFICE_ROLES
    )


# ─── MCP-тул: awf_add_role(preset=...) ───────────────────────────────────

_mcp_only = pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp not available")


@_mcp_only
def test_mcp_tool_preset_office(tmp_git_repo, monkeypatch):
    """MCP-уровень: preset + models → status ok и таблица в ответе."""
    _office_env(tmp_git_repo, monkeypatch)
    result = _run(awf_mod.awf_add_role(
        preset="office",
        project_dir=str(tmp_git_repo),
        models={"qa-review": "vllm/llm"},
    ))
    assert result["status"] == "ok"
    assert result["preset"] == "office"
    assert len(result["created"]) == len(OFFICE_ROLES)
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["role"] == "product"
    assert "agent-product" in result["skipped"][0]["reason"]
    row = {r["role"]: r for r in result["models"]}["qa-review"]
    assert row == {
        "role": "qa-review",
        "model": "vllm/llm",
        "locus": "models.qa-review.model",
        "action": "written",
    }
    assert _config(tmp_git_repo)["models"]["qa-review"]["model"] == "vllm/llm"


@_mcp_only
def test_mcp_tool_preset_exclusive_with_single_role_params(tmp_git_repo):
    """preset взаимноисключаем с одиночными параметрами роли."""
    api.init_project(tmp_git_repo, project_name="Exclusive")
    result = _run(awf_mod.awf_add_role(
        preset="office",
        name="qa",
        project_dir=str(tmp_git_repo),
    ))
    assert result["status"] == "error"
    assert "preset" in result["error"]
    # ничего не создано (supervisor.md от init не в счёт)
    roles_dir = tmp_git_repo / ".agentic" / "roles"
    assert not any(
        (roles_dir / f"{role}.md").is_file() for role in OFFICE_ROLES
    )


@_mcp_only
def test_mcp_tool_docstring_documents_preset(tmp_git_repo):
    """Требование 5: докстринг тула описывает preset (короткий пример)."""
    doc = awf_mod.awf_add_role.__doc__ or ""
    assert 'preset="office"' in doc
    assert "models=" in doc
    assert "awf_add_role" in doc  # пример вызова
