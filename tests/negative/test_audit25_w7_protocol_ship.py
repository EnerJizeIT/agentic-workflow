"""W7 (аудит 2026-09-25, волна 7): стратегия supervisor шиппится с awf.

До: QA-протокол (опровергаешь, не подтверждаешь; шесть вопросов;
находка = красный тест) и протокол исполнителя (контракт TODO,
prove_red, DONE-факты) жили только в `.agentic/roles/` этого
репозитория — в чужих проектах воспроизвести их было нельзя.
`awf init` не создавал `.agentic/doctrine/`, а роль нельзя было
запустить без прав edit/write.

Покрывают:
- test_role_templates_carry_protocol_markers — маркеры протоколов в
  шипующихся шаблонах
- test_add_role_ships_builtin_templates — add-role кладёт встроенный
  шаблон, а не плейсхолдер
- test_add_role_other_names_keep_placeholder — имена прежних веток
  (включая supervisor) ведут себя как раньше
- test_add_role_missing_builtin_falls_back_to_placeholder — старой
  установки без файла шаблон деградирует в плейсхолдер
- test_env_readonly_role_denies_edit_write — readonly-роль с edit/write=deny (V-04)
- test_env_normal_role_keeps_edit_write — обычная роль со всем как раньше
- test_env_empty_readonly_list_keeps_previous_behavior — пустой список =
  прежнее поведение
- test_init_seeds_doctrine_templates — init на пустом проекте создаёт
  доктрину
- test_reinit_does_not_overwrite_user_doctrine — повторный init не
  перезаписывает файл, изменённый пользователем
- test_supervisor_template_has_quality_bar — секция качества в шаблоне
  supervisor
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import api
from awf._env import awf_subprocess_env

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "awf" / "templates"

DOCTRINE_NAMES = (
    "01-process-group.md",
    "02-worker-tree.md",
    "03-launch-lease.md",
)


def _permission(env: dict) -> dict:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]


def _env_project(base: Path, monkeypatch, yaml_text: str, name: str) -> Path:
    """Проект с .agentic/config.yaml и изолированным XDG (без user's opencode.json)."""
    proj = base / name
    ag = proj / ".agentic"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg-nothing"))
    return proj


# ─── 1. Маркеры протоколов в шаблонах ───────────────────────────────────


def test_role_templates_carry_protocol_markers():
    """Ревьюер и исполнитель несут протокол, а не каркас."""
    qa = (TEMPLATES / "roles" / "agent-qa-review.md").read_text(encoding="utf-8")
    for marker in (
        "опровергаешь, не подтверждаешь",
        "Шесть вопросов",
        "Адресные сценарии",
        "Находка = красный тест",
        "Прод-код не правишь",
    ):
        assert marker in qa, f"шаблон agent-qa-review потерял маркер: {marker}"

    impl = (TEMPLATES / "roles" / "agent-implementer.md").read_text(encoding="utf-8")
    for marker in (
        "контракт TODO",
        "prove_red",
        "git commit",
        "git add",
        "DONE-{id}.json",
    ):
        assert marker in impl, f"шаблон agent-implementer потерял маркер: {marker}"


def test_supervisor_template_has_quality_bar():
    """Шаблон supervisor несёт правила качества (дополнение к W7)."""
    text = (TEMPLATES / "roles" / "supervisor.md").read_text(encoding="utf-8")
    for marker in (
        "Quality bar",
        "prove_red",
        "Open P1 blocks release",
        "one invariant",
    ):
        assert marker in text, f"шаблон supervisor потерял маркер качества: {marker}"


# ─── 2. add-role кладёт встроенный шаблон ───────────────────────────────


def _without_model_line(text: str) -> str:
    """Убирает единообразную строку модели (TODO-0175) и строку после неё.

    Шаблон в репо не обязан содержать плейсхолдер — строка инжектится
    после копирования, поэтому равенство тела проверяется после
    её вычета.
    """
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("**Модель:**"):
            del lines[i]
            if i < len(lines) and lines[i] == "":
                del lines[i]
            break
    return "\n".join(lines)


def test_add_role_ships_builtin_templates(tmp_git_repo):
    """add-role кладёт шипующийся шаблон; строка модели добавлена
    (TODO-0175), тело шаблона не искажено."""
    api.init_project(tmp_git_repo, project_name="W7Ship")
    for name in ("agent-qa-review", "agent-implementer"):
        built_in = (TEMPLATES / "roles" / f"{name}.md").read_text(encoding="utf-8")
        result = api.add_role(tmp_git_repo, name)
        role_file = tmp_git_repo / ".agentic" / "roles" / f"{name}.md"
        assert result.role_name == name
        assert role_file.is_file()
        content = role_file.read_text(encoding="utf-8")
        assert "**Модель:**" in content
        assert _without_model_line(content) == built_in


def test_add_role_other_names_keep_placeholder(tmp_git_repo):
    """Имена прежних веток (включая supervisor) дают плейсхолдер, как раньше."""
    api.init_project(tmp_git_repo, project_name="W7Ship")

    result = api.add_role(tmp_git_repo, "qa", description="QA", model="m1")
    content = (tmp_git_repo / ".agentic" / "roles" / "qa.md").read_text(encoding="utf-8")
    assert "ROLE: qa" in content
    assert "## 1. Who you are" in content
    assert "m1" in content

    # supervisor: его шаблон — для init, не для add-role
    (tmp_git_repo / ".agentic" / "roles" / "supervisor.md").unlink()
    api.add_role(tmp_git_repo, "supervisor")
    sup = (tmp_git_repo / ".agentic" / "roles" / "supervisor.md").read_text(encoding="utf-8")
    assert "ROLE: supervisor" in sup
    assert "## 1. Who you are" in sup


def test_add_role_missing_builtin_falls_back_to_placeholder(tmp_git_repo, monkeypatch):
    """Старая установка без файла шаблона деградирует в прежний плейсхолдер."""
    monkeypatch.setattr("awf.api.roles._builtin_role_template", lambda name: None)
    api.init_project(tmp_git_repo, project_name="W7Ship")
    api.add_role(tmp_git_repo, "agent-qa-review")
    content = (
        tmp_git_repo / ".agentic" / "roles" / "agent-qa-review.md"
    ).read_text(encoding="utf-8")
    assert "ROLE: agent-qa-review" in content
    assert "## 1. Who you are" in content


# ─── 3. readonly_roles: env без edit/write ──────────────────────────────

_READONLY_YAML = "automation:\n  readonly_roles:\n    - agent-qa-review\n"


def test_env_readonly_role_denies_edit_write(tmp_path, monkeypatch):
    """Роль из automation.readonly_roles: edit/write=deny (V-04), bash остаётся."""
    proj = _env_project(tmp_path, monkeypatch, _READONLY_YAML, "ro")
    env = awf_subprocess_env(role="agent-qa-review", project_dir=proj)
    perm = _permission(env)
    assert perm["edit"] == "deny"
    assert perm["write"] == "deny"
    assert perm["bash"] == "allow"
    assert perm["webfetch"] == "allow"


def test_env_normal_role_keeps_edit_write(tmp_path, monkeypatch):
    """Роль, не входящая в список, получает всё, как раньше."""
    proj = _env_project(tmp_path, monkeypatch, _READONLY_YAML, "normal")
    env = awf_subprocess_env(role="agent-implementer", project_dir=proj)
    perm = _permission(env)
    assert perm["edit"] == "allow"
    assert perm["write"] == "allow"
    assert perm["bash"] == "allow"


def test_env_empty_readonly_list_keeps_previous_behavior(tmp_path, monkeypatch):
    """Пустой список (и отсутствующий ключ) = прежнее поведение."""
    for i, yaml_text in enumerate(
        ("automation:\n  readonly_roles: []\n", "project:\n  name: x\n")
    ):
        proj = _env_project(tmp_path, monkeypatch, yaml_text, f"empty{i}")
        env = awf_subprocess_env(role="agent-qa-review", project_dir=proj)
        perm = _permission(env)
        assert perm["edit"] == "allow", yaml_text
        assert perm["write"] == "allow", yaml_text

    # вызов без роли/проекта — ровно как было до W7
    env = awf_subprocess_env()
    perm = _permission(env)
    assert perm["edit"] == "allow"
    assert perm["write"] == "allow"
    assert perm["bash"] == "allow"


# ─── 4. Доктрина в поставке: init сеет, re-init не затирает ─────────────


def test_init_seeds_doctrine_templates(tmp_git_repo):
    """Init на пустом проекте создаёт .agentic/doctrine/ со штих-доктриной."""
    api.init_project(tmp_git_repo, project_name="W7Doctrine")
    doctrine = tmp_git_repo / ".agentic" / "doctrine"
    assert doctrine.is_dir()
    for name in DOCTRINE_NAMES:
        f = doctrine / name
        assert f.is_file(), f"init не посевил доктрину: {name}"
        assert f.read_text(encoding="utf-8").startswith("# ")


def test_reinit_does_not_overwrite_user_doctrine(tmp_git_repo):
    """Повторный init не перезаписывает изменённый файл, недостающий досевивает."""
    api.init_project(tmp_git_repo, project_name="W7Doctrine")
    doctrine = tmp_git_repo / ".agentic" / "doctrine"
    user_edit = "# Сигналы — только в свою группу процессов\n\n(правка владельца)\n"
    (doctrine / "01-process-group.md").write_text(user_edit, encoding="utf-8")
    (doctrine / "03-launch-lease.md").unlink()

    api.init_project(tmp_git_repo, project_name="W7Doctrine")

    assert (doctrine / "01-process-group.md").read_text(encoding="utf-8") == user_edit
    assert (doctrine / "03-launch-lease.md").is_file()
