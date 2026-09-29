"""ORCH M7.2: профили tools по поручению стадии (allow/deny).

До: схема стадии (ALLOWED_STAGE_KEYS, A-06) отвергала ``tools`` как
неизвестный ключ — поручение не могло сузить набор tools: аналитик
получал edit/write, а сузить набор поручением было нельзя.

Механизм (без нового, тот же что M2.2): permission-ключи в
``OPENCODE_CONFIG_CONTENT``. Профиль мержится поверх базовых правил
(host-конфиг + awf-оверрайд + readonly_roles); управляющие tools
M2.2 для execute — locked deny: deny сильнее allow, базовые
управляющие запреты профилем не отменяются (allow управляющего
tool не снимает M2.2).

Реестр известных ключей (единый, как схема M3.1): встроенные
``bash``/``edit``/``write``/``webfetch`` + MCP-tools вида
``<server>_<tool>``. Неизвестный ключ — ошибка загрузки пайплайна.

Профиль сужает случайные возможности стадии — не граница
безопасности (bash остаётся, если профиль его явно не запретил).

Покрывают:
- test_stage_tools_profile_narrows_to_allowed — профиль сужает
  набор стадии до allow-списка (prove_red: до фикса ``tools`` —
  unknown key в схеме)
- test_tools_unknown_key_rejected_at_load — неизвестный ключ /
  плохой тип → ошибка загрузки (единый реестр)
- test_tools_profile_mcp_key_accepted — ключ ``<server>_<tool>``
  проходит реестр
- test_profile_cannot_reenable_control_tools — allow управляющего
  tool на execute не отменяет M2.2 (locked deny + agent-блок)
- test_profile_deny_beats_allow — ключ в обоих списках → deny
- test_readonly_profile_cannot_reenable_edit — профиль не отменяет
  V-04 (readonly edit/write)
- test_no_tools_profile_unchanged — legacy YAML: профиль пуст,
  конфиг как раньше (back-compat)
- test_profile_applies_to_plan_stage — объявлено на plan —
  применяется (M2.2 в стадию супервизора не вмешивается)
- test_snapshot_roundtrip_carries_tools — снапшот несёт профиль
  (один ruleset load/write)
- test_write_path_same_registry — write-путь: тот же валидатор
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf._env import awf_subprocess_env
from awf.api._errors import AwfApiError
from awf.pipeline import load_stages, pipeline_snapshot_text

MCP = "agent-workflow-ui"

# Execute-стадия с профилем: набор сужается до allow-списка
# (bash/webfetch остаются), edit/write запрещены.
PROFILE_YAML = (
    "stages:\n"
    "  - name: plan\n    role: supervisor\n"
    "  - name: analyst\n    role: analyst\n"
    "    tools:\n"
    "      allow: [webfetch, bash]\n"
    "      deny: [edit, write]\n"
    "  - name: verify\n    role: supervisor\n"
)

LEGACY_YAML = (
    "stages:\n"
    "  - name: plan\n    role: supervisor\n"
    "  - name: impl\n    role: worker\n"
    "  - name: verify\n    role: supervisor\n"
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pipeline.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _env_project(
    base: Path, monkeypatch, name: str, config_text: str = "",
    user_config: str | None = None,
) -> Path:
    """Проект с .agentic/ и изолированным XDG (без чужого opencode.json)."""
    proj = base / name
    (proj / ".agentic").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        config_text or "project:\n  name: test\n", encoding="utf-8"
    )
    xdg = base / "xdg" / name
    (xdg / "opencode").mkdir(parents=True)
    if user_config is not None:
        (xdg / "opencode" / "opencode.json").write_text(
            user_config, encoding="utf-8"
        )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return proj


def _config(env: dict) -> dict:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])


def _stage_with_tools(tools_yaml: str) -> str:
    return (
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: analyst\n    role: analyst\n"
        f"{tools_yaml}"
        "  - name: verify\n    role: supervisor\n"
    )


# ── prove_red: профиль сужает набор стадии ─────────────────────────────────


def test_stage_tools_profile_narrows_to_allowed(tmp_path, monkeypatch):
    """ORCH M7.2: профиль сужает набор стадии до allow-списка.

    До фикса ``tools`` отвергается в схеме как unknown key — цепочка
    load → env не проходит.
    """
    stages = load_stages(_write(tmp_path, PROFILE_YAML))
    assert stages[1].tools == {
        "allow": ["webfetch", "bash"], "deny": ["edit", "write"],
    }

    proj = _env_project(tmp_path, monkeypatch, "narrow")
    env = awf_subprocess_env(
        role="analyst",
        project_dir=proj,
        agent_name="worker",
        restrict_control_tools=True,
        tools_profile=stages[1].tools,
    )
    perm = _config(env)["permission"]
    # сужение: ровно allow-список жив, edit/write — deny
    assert perm["bash"] == "allow"
    assert perm["webfetch"] == "allow"
    assert perm["edit"] == "deny"
    assert perm["write"] == "deny"
    # M2.2 цел: управляющие tools запрещены в agent-блоке
    agent_perm = _config(env)["agent"]["worker"]["permission"]
    for tool in ("awf_approve", "awf_kill"):
        assert agent_perm[f"{MCP}_{tool}"] == "deny"


# ── Валидация: единый реестр ключей (ошибка загрузки) ─────────────────────


@pytest.mark.parametrize(
    "tools_yaml, match",
    [
        ("    tools:\n      deny: [read]\n", "unknown permission key"),
        ("    tools:\n      allow: [edit x]\n", "unknown permission key"),
        (
            "    tools:\n      allow: [bash]\n      extra: []\n",
            r"unknown keys: \['extra'\]",
        ),
        ("    tools: [bash, edit]\n", "must be a mapping"),
        ("    tools:\n      allow: edit\n", "must be a list"),
        ("    tools:\n      allow: [42]\n", "must be a list"),
    ],
)
def test_tools_unknown_key_rejected_at_load(tmp_path, tools_yaml, match):
    """ORCH M7.2: неизвестный ключ / плохой тип — AwfApiError при
    загрузке, не тихий no-op в рантайме."""
    with pytest.raises(AwfApiError, match=match):
        load_stages(_write(tmp_path, _stage_with_tools(tools_yaml)))


def test_tools_profile_mcp_key_accepted(tmp_path):
    """ORCH M7.2: MCP-ключ ``<server>_<tool>`` — известный (реестр)."""
    content = _stage_with_tools(
        f"    tools:\n      deny: [{MCP}_awf_status, {MCP}_awf_kill]\n"
    )
    stages = load_stages(_write(tmp_path, content))

    assert stages[1].tools == {
        "deny": [f"{MCP}_awf_status", f"{MCP}_awf_kill"],
    }


# ── Мерж: базовые запреты не отменяются ────────────────────────────────────


def test_profile_cannot_reenable_control_tools(tmp_path, monkeypatch):
    """ORCH M7.2: allow управляющего tool не отменяет M2.2 на
    execute — locked deny и в top-level, и в agent-блоке."""
    content = _stage_with_tools(
        f"    tools:\n      allow: [{MCP}_awf_approve]\n"
    )
    stages = load_stages(_write(tmp_path, content))
    proj = _env_project(tmp_path, monkeypatch, "locked")
    env = awf_subprocess_env(
        role="analyst",
        project_dir=proj,
        agent_name="worker",
        restrict_control_tools=True,
        tools_profile=stages[1].tools,
    )
    assert _config(env)["permission"][f"{MCP}_awf_approve"] == "deny"
    agent_perm = _config(env)["agent"]["worker"]["permission"]
    assert agent_perm[f"{MCP}_awf_approve"] == "deny"


def test_profile_deny_beats_allow(tmp_path, monkeypatch):
    """ORCH M7.2: ключ в allow и в deny — результат deny."""
    proj = _env_project(tmp_path, monkeypatch, "denywins")
    env = awf_subprocess_env(
        role="analyst",
        project_dir=proj,
        tools_profile={"allow": ["webfetch"], "deny": ["webfetch"]},
    )
    assert _config(env)["permission"]["webfetch"] == "deny"


def test_readonly_profile_cannot_reenable_edit(tmp_path, monkeypatch):
    """ORCH M7.2: readonly edit/write (V-04) — базовый запрет,
    профиль его не отменяет; bash остаётся."""
    cfg = (
        "project:\n  name: test\n"
        "automation:\n  readonly_roles: [analyst]\n"
    )
    proj = _env_project(tmp_path, monkeypatch, "ro", cfg)
    env = awf_subprocess_env(
        role="analyst",
        project_dir=proj,
        tools_profile={"allow": ["edit", "write"]},
    )
    perm = _config(env)["permission"]
    assert perm["edit"] == "deny"
    assert perm["write"] == "deny"
    assert perm["bash"] == "allow"


# ── Back-compat и применение по объявлению ─────────────────────────────────


def test_no_tools_profile_unchanged(tmp_path, monkeypatch):
    """ORCH M7.2: без ``tools`` — поведение как раньше (пустой
    профиль = отсутствие; base execute-правила на месте)."""
    stages = load_stages(_write(tmp_path, LEGACY_YAML))
    assert all(st.tools == {} for st in stages)

    proj = _env_project(tmp_path, monkeypatch, "legacy")
    env_bare = awf_subprocess_env(role="analyst", project_dir=proj)
    env_empty = awf_subprocess_env(
        role="analyst", project_dir=proj, tools_profile={},
    )
    assert env_bare["OPENCODE_CONFIG_CONTENT"] == env_empty[
        "OPENCODE_CONFIG_CONTENT"
    ]
    env_exec = awf_subprocess_env(
        role="analyst",
        project_dir=proj,
        agent_name="worker",
        restrict_control_tools=True,
        tools_profile={},
    )
    perm = _config(env_exec)["permission"]
    assert (perm["bash"], perm["edit"], perm["write"]) == (
        "allow", "allow", "allow",
    )


def test_profile_applies_to_plan_stage(tmp_path, monkeypatch):
    """ORCH M7.2: профиль по объявлению — на plan-стадии применяется;
    M2.2 в стадию супервизора не вмешивается (agent-блока нет)."""
    content = (
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "    tools:\n      deny: [webfetch]\n"
        "  - name: impl\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n"
    )
    stages = load_stages(_write(tmp_path, content))
    assert stages[0].tools == {"deny": ["webfetch"]}

    proj = _env_project(tmp_path, monkeypatch, "plan")
    env = awf_subprocess_env(
        role="supervisor",
        project_dir=proj,
        agent_name="supervisor",
        tools_profile=stages[0].tools,
    )
    config = _config(env)
    assert config["permission"]["webfetch"] == "deny"
    assert config["permission"]["bash"] == "allow"
    assert "agent" not in config


# ── Один ruleset: снапшот и write-путь ─────────────────────────────────────


def test_snapshot_roundtrip_carries_tools(tmp_path):
    """ORCH M7.2: снапшот M3.3 несёт профиль — тот же лоадер,
    тот же результат."""
    stages = load_stages(_write(tmp_path, PROFILE_YAML))
    text = pipeline_snapshot_text(stages, "default", "S001")
    snap = tmp_path / "snapshot.yaml"
    snap.write_text(text, encoding="utf-8")

    again = load_stages(snap)
    assert [s.tools for s in again] == [s.tools for s in stages]


def test_write_path_same_registry(tmp_path, monkeypatch):
    """ORCH M7.2: write-путь — тот же валидатор (единый ruleset);
    неизвестный ключ в профиле — отказ, валидный — roundtrip."""
    from awf import api

    proj = _env_project(tmp_path, monkeypatch, "w")

    with pytest.raises(AwfApiError, match="unknown permission key"):
        api.write_pipeline(
            proj, "p",
            [
                {"role": "supervisor", "name": "plan"},
                {"role": "analyst", "tools": {"deny": ["read"]}},
                {"role": "supervisor", "name": "verify"},
            ],
        )
    api.write_pipeline(
        proj, "p",
        [
            {"role": "supervisor", "name": "plan"},
            {
                "role": "analyst",
                "tools": {"allow": ["bash"], "deny": ["edit"]},
            },
            {"role": "supervisor", "name": "verify"},
        ],
    )
    data = yaml.safe_load(
        (proj / ".agentic" / "pipelines" / "p.yaml").read_text(encoding="utf-8")
    )
    assert data["stages"][1]["tools"] == {"allow": ["bash"], "deny": ["edit"]}
    stages = load_stages(proj / ".agentic" / "pipelines" / "p.yaml")
    assert stages[1].tools == {"allow": ["bash"], "deny": ["edit"]}
