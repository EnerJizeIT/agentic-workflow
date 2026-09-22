"""RUN6 #5 (TODO-0060): the awf-supervisor doctrine skill.

The supervisor doctrine (role, cycle, rituals, tool map, five scenarios,
defaults) is a GLOBAL skill installed with the plugin — the same
self-healing lazy-install mechanism as the plugin's own SKILL.md.
"""
from __future__ import annotations

from pathlib import Path

from agent_workflow_ui import skill_installer


def _bundled() -> Path:
    return (
        Path(skill_installer.__file__).resolve().parent
        / skill_installer.SUPERVISOR_SKILL_FILENAME
    )


class TestSkillContent:
    def test_frontmatter_name_and_trigger(self):
        text = _bundled().read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "name: awf-supervisor" in text
        # The owner's trigger phrase (TODO: «работай супервизором awf»).
        assert "работай супервизором awf" in text

    def test_five_scenarios(self):
        text = _bundled().read_text(encoding="utf-8")
        for marker in (
            "### S1. Одиночная задача",
            "### S2. Забег",
            "### S3. Verify-ритуал",
            "### S4. Blocked",
            "### S5. Salvage",
        ):
            assert marker in text

    def test_scenarios_have_situation_calls_decision(self):
        text = _bundled().read_text(encoding="utf-8")
        assert "Ситуация" in text
        assert "Решение" in text
        for call in (
            "awf_dispatch_todo",
            "awf_run_start",
            "awf_run_next",
            "awf_approve",
            "awf_reject",
            "awf_continue",
            "awf_retry_stage",
        ):
            assert call in text

    def test_role_cycle_rituals_defaults_map(self):
        text = _bundled().read_text(encoding="utf-8")
        for section in (
            "## Цикл",
            "## Дефолты",
            "## Ритуалы",
            "## Пять сценариев",
            "## Карта инструментов",
            "## Запреты супервизора",
        ):
            assert section in text

    def test_brief_is_the_live_state(self):
        # The skill is doctrine; live state stays in awf_brief.
        assert "awf_brief" in _bundled().read_text(encoding="utf-8")


class TestSkillInstall:
    def test_install_copies_skill_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert skill_installer.ensure_supervisor_skill_installed() is True
        target = (
            tmp_path / "opencode" / "skills" / "awf-supervisor" / "SKILL.md"
        )
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == _bundled().read_text(
            encoding="utf-8"
        )

    def test_install_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert skill_installer.ensure_supervisor_skill_installed() is True
        assert skill_installer.ensure_supervisor_skill_installed() is True

    def test_updates_drifted_skill(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        target = tmp_path / "opencode" / "skills" / "awf-supervisor" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("STALE CONTENT", encoding="utf-8")
        assert skill_installer.ensure_supervisor_skill_installed() is True
        assert target.read_text(encoding="utf-8") != "STALE CONTENT"

    def test_package_data_ships_the_skill(self):
        pyproject = (
            Path(__file__).resolve().parents[2] / "agent_workflow_ui" / "pyproject.toml"
        )
        assert "SKILL.awf-supervisor.md" in pyproject.read_text(encoding="utf-8")


class TestMainWiring:
    def test_startup_runs_all_three_installs_in_order(self, monkeypatch):
        import agent_workflow_ui.__main__ as m

        calls: list[str] = []
        monkeypatch.setattr(m, "ensure_skill_installed", lambda: calls.append("skill"))
        monkeypatch.setattr(
            m, "ensure_supervisor_skill_installed", lambda: calls.append("supervisor")
        )
        monkeypatch.setattr(m, "ensure_agents_md", lambda: calls.append("agents_md"))

        def _stop(*a, **k):
            raise RuntimeError("stop-after-installs")

        monkeypatch.setattr(m, "start_http_server", _stop)
        # main() swallows the stop signal and returns 1 (FATAL path) — the
        # install calls above it are what we assert on.
        assert m.main() == 1
        assert calls == ["skill", "supervisor", "agents_md"]
