"""V-04 (повторная проверка 27.09, audit gpt6-sol-xhigh 16-supervisor).

До: для роли из `automation.readonly_roles` awf лишь «не добавлял»
override `edit/write=allow` — значения пользовательского `opencode.json`
сохранялись. Синтетический конфиг с `edit=allow, write=allow` давал
ревьюеру `allow/allow` плюс всегда разрешённый `bash`.

Решение владельца (27.09): жёсткий запрет `edit`/`write` поверх любых
настроек хоста; `bash` остаётся (QA прогоняет тесты).

Покрывают:
- test_listed_role_denied_despite_user_allow — роль из списка получает
  `edit: deny, write: deny` поверх пользовательских `allow`
- test_unlisted_role_keeps_user_permissions — роль не из списка:
  пользовательские `allow` сохраняются
- test_empty_readonly_list_config_untouched — пустой список: пользовательский
  конфиг не трогается
- test_readonly_role_keeps_bash — `bash` у read-only роли разрешён
"""
from __future__ import annotations

import json
from pathlib import Path

from awf._env import awf_subprocess_env

READONLY_YAML = "automation:\n  readonly_roles:\n    - agent-qa-review\n"
USER_ALLOW_JSON = json.dumps({"permission": {"edit": "allow", "write": "allow"}})


def _permission(env: dict) -> dict:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]


def _env_project(
    base: Path,
    monkeypatch,
    yaml_text: str,
    name: str,
    user_config: str | None = None,
) -> Path:
    """Проект с .agentic/config.yaml и изолированным XDG (user's opencode.json)."""
    proj = base / name
    ag = proj / ".agentic"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text(yaml_text, encoding="utf-8")
    xdg = base / "xdg" / name
    (xdg / "opencode").mkdir(parents=True)
    if user_config is not None:
        (xdg / "opencode" / "opencode.json").write_text(user_config, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return proj


def test_listed_role_denied_despite_user_allow(tmp_path, monkeypatch):
    """Роль из списка: пользовательские `allow` перекрываются `deny/deny`."""
    proj = _env_project(tmp_path, monkeypatch, READONLY_YAML, "ro", USER_ALLOW_JSON)
    env = awf_subprocess_env(role="agent-qa-review", project_dir=proj)
    perm = _permission(env)
    assert perm["edit"] == "deny"
    assert perm["write"] == "deny"
    assert perm["bash"] == "allow"


def test_unlisted_role_keeps_user_permissions(tmp_path, monkeypatch):
    """Роль не из списка: пользовательские `allow` сохранены."""
    proj = _env_project(tmp_path, monkeypatch, READONLY_YAML, "normal", USER_ALLOW_JSON)
    env = awf_subprocess_env(role="agent-implementer", project_dir=proj)
    perm = _permission(env)
    assert perm["edit"] == "allow"
    assert perm["write"] == "allow"
    assert perm["bash"] == "allow"


def test_empty_readonly_list_config_untouched(tmp_path, monkeypatch):
    """Пустой список (и отсутствующий ключ): пользовательский конфиг не трогается."""
    for i, yaml_text in enumerate(
        ("automation:\n  readonly_roles: []\n", "project:\n  name: x\n")
    ):
        proj = _env_project(tmp_path, monkeypatch, yaml_text, f"empty{i}", USER_ALLOW_JSON)
        env = awf_subprocess_env(role="agent-qa-review", project_dir=proj)
        perm = _permission(env)
        assert perm["edit"] == "allow", yaml_text
        assert perm["write"] == "allow", yaml_text
        assert perm["bash"] == "allow", yaml_text


def test_readonly_role_keeps_bash(tmp_path, monkeypatch):
    """`bash` у read-only роли разрешён (осознанно: QA прогоняет тесты)."""
    proj = _env_project(tmp_path, monkeypatch, READONLY_YAML, "bash", USER_ALLOW_JSON)
    env = awf_subprocess_env(role="agent-qa-review", project_dir=proj)
    perm = _permission(env)
    assert perm["bash"] == "allow"
