"""Tests for `awf brief` (RUN4 #1, TODO-0051) — the supervisor's
onboarding/recovery card.

Covers:
- every card section (header, what's next, state, tool map, rituals,
  recovery, doctrine, what's new, feedback)
- tool map / registry sync in BOTH directions (every registry tool is in
  the map; every map entry exists in the registry)
- live vs new project (setup chain vs working cycle)
- word budget (≤900)
- determinism (two calls, same state — identical except the date line)
- --json (machine variant)
- empty/nonexistent project (no crash)
- supervisor.md prompt line (both template copies) + USAGE paragraphs
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from awf import api
from awf import brief as brief_mod
from awf.brief import (
    MAX_WORDS,
    _clip_by_words,
    latest_changelog,
    load_recovery,
    load_tool_map,
    map_coverage,
    tool_registry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SUPERVISOR_TEMPLATE_COPIES = (
    "awf/templates/roles/supervisor.md",
    "templates/roles/supervisor.md",
)


# ─── Fixtures ────────────────────────────────────────────────────────────


def _setup_project(tmp_path: Path, name: str = "proj") -> Path:
    project = tmp_path / name
    (project / ".agentic").mkdir(parents=True)
    (project / ".agentic" / "state").mkdir(parents=True)
    return project


def _add_pipeline(project: Path) -> None:
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "name: default\nstages:\n"
        "  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: worker\n    role: worker\n    kind: execute\n"
        "  - name: verify\n    role: supervisor\n    kind: verify\n",
        encoding="utf-8",
    )


def _make_live(project: Path) -> None:
    """Live project (RUN3-7): configured + at least one archived TODO."""
    _add_pipeline(project)
    done = project / ".agentic" / "done" / "TODO-0001"
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# TODO-0001\n", encoding="utf-8")


def _make_active_todo(project: Path, todo_id: str = "TODO-0042") -> None:
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").touch()


@pytest.fixture
def live_project(tmp_path: Path) -> Path:
    project = _setup_project(tmp_path)
    _make_live(project)
    return project


# ─── Tool registry + map coverage (both directions) ─────────────────────


class TestToolRegistry:
    def test_registry_has_both_sides(self):
        reg = tool_registry()
        assert "awf_start" in reg["mcp"]
        assert "awf_brief" in reg["mcp"]
        assert "open_form" in reg["mcp"]
        assert "start" in reg["cli"]
        assert "brief" in reg["cli"]
        assert "tree-sha" in reg["cli"]

    def test_map_covers_every_registry_tool(self):
        """(a) every registry tool (MCP + CLI) is in at least one group."""
        cov = map_coverage()
        assert cov["missing_from_map"] == [], (
            f"registry tools missing from tool_map.yaml: {cov['missing_from_map']}"
        )

    def test_map_entries_exist_in_registry(self):
        """(b) every map entry exists in the live registry."""
        cov = map_coverage()
        assert cov["unknown_in_map"] == [], (
            f"map entries not in the registry: {cov['unknown_in_map']}"
        )

    def test_run3_run4_tools_present(self):
        """The new RUN3/RUN4 tools must be in the map."""
        flat = {
            (t["mcp"], t["cli"])
            for g in load_tool_map()
            for t in g["tools"]
        }
        for mcp_name in (
            "awf_pipelines",
            "awf_write_pipeline",
            "awf_unblock",
            "awf_todo_remove",
            "awf_feedback",
            "awf_metrics",
            "awf_brief",
        ):
            assert any(m == mcp_name for m, _ in flat), f"{mcp_name} not in map"
        for cli_name in ("tree-sha", "mutations", "todo-draft"):
            assert any(c == cli_name for _, c in flat), f"{cli_name} not in map"

    def test_every_entry_has_a_when_line(self):
        for g in load_tool_map():
            for t in g["tools"]:
                assert t["when"], f"empty 'when' in group {g['title']}"
                assert t["mcp"] or t["cli"], "entry needs mcp or cli name"


class TestClip:
    def test_short_text_unchanged(self):
        text, clipped = _clip_by_words("one two three", 10)
        assert text == "one two three"
        assert clipped is False

    def test_single_long_line_cut_at_word(self):
        text = " ".join(f"w{i}" for i in range(50))
        clipped_text, clipped = _clip_by_words(text, 10)
        assert clipped is True
        assert len(clipped_text.split()) == 10

    def test_multiline_cut_keeps_earlier_lines(self):
        text = "a b c\n" + "d " * 20 + "e"
        clipped_text, clipped = _clip_by_words(text, 10)
        assert clipped is True
        assert clipped_text.startswith("a b c")
        assert len(clipped_text.split()) <= 10


# ─── Card sections (live project) ───────────────────────────────────────


class TestCardSections:
    def test_header(self, live_project):
        r = api.brief(live_project)
        assert r.version
        assert r.project == "proj"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date)
        assert r.phase in ("run", "verify", "brief")
        first = r.text.splitlines()[0]
        assert first.startswith("# awf brief —")
        assert r.version in r.text.splitlines()[2]

    def test_what_next_section(self, live_project):
        r = api.brief(live_project)
        assert "## What's next" in r.text
        assert r.is_live_project
        assert "New project — setup chain" not in r.text
        assert r.live_line

    def test_state_section(self, live_project):
        _make_active_todo(live_project)
        r = api.brief(live_project)
        assert "## State" in r.text
        assert "TODO-0042" in r.text
        assert "no active run" not in r.text

    def test_state_empty_fallback(self, live_project):
        r = api.brief(live_project)
        assert "- no active run, tasks, or flags" in r.text

    def test_tool_map_section(self, live_project):
        r = api.brief(live_project)
        assert "## Tool map" in r.text
        for title in ("launch", "check", "run", "hygiene", "forms", "metrics", "context"):
            assert f"**{title}**" in r.text
        assert "`awf_start`/`start`" in r.text
        assert "`awf tree-sha`" in r.text

    def test_rituals_section(self, live_project):
        r = api.brief(live_project)
        assert "## Rituals" in r.text
        assert "awf tree-sha" in r.text
        assert "verified_sha" in r.text
        assert "awf_run_finish" in r.text
        assert "awf_unblock" in r.text

    def test_rituals_run_loop_done_line(self, live_project):
        """RUN6 #1: the card teaches the approve → done → run_next loop."""
        r = api.brief(live_project)
        assert "after approve the pipeline exits" in r.text
        assert "`done`" in r.text

    def test_recovery_section(self, live_project):
        r = api.brief(live_project)
        assert "## Recovery" in r.text
        for recipe_tool in ("awf_unblock", "awf_restore", "awf_todo_remove", "awf_retry_stage"):
            assert recipe_tool in r.text
        assert "no_checkpoints" in r.text
        assert load_recovery() in r.text

    def test_recovery_approved_recipe(self, live_project):
        """RUN6 #1: the 'approved, where's the next step?' recipe is in the card."""
        r = api.brief(live_project)
        assert "where's the next step?" in r.text

    def test_doctrine_section(self, live_project):
        doctrine = live_project / ".agentic" / "doctrine"
        doctrine.mkdir(parents=True)
        (doctrine / "01-test.md").write_text(
            "# Тест-доктрина\n\nТело.\n", encoding="utf-8"
        )
        r = api.brief(live_project)
        assert "**Doctrine**" in r.text
        assert "01-test.md" in r.text
        assert "Тест-доктрина" in r.text

    def test_what_new_section(self, live_project):
        r = api.brief(live_project)
        assert "## What's new" in r.text
        section = latest_changelog()
        if section:  # repo layout: CHANGELOG.md is present
            assert "## [Unreleased]" in r.text

    def test_feedback_line(self, live_project):
        r = api.brief(live_project)
        assert "## Feedback" in r.text
        assert "awf feedback --type bug|feature" in r.text

    def test_section_order(self, live_project):
        r = api.brief(live_project)
        heads = [ln for ln in r.text.splitlines() if ln.startswith("## ")]
        names = [h[3:] for h in heads]
        assert names == [
            "What's next",
            "State",
            "Tool map",
            "Rituals",
            "Recovery",
            "What's new",
            "Feedback",
        ]


# ─── Live vs new project ─────────────────────────────────────────────────


class TestLiveVsNew:
    def test_live_project_no_setup_chain(self, live_project):
        r = api.brief(live_project)
        assert r.is_live_project
        assert r.setup_hint == ""
        assert "working cycle" in r.live_line

    def test_new_project_gets_setup_hint(self, tmp_path):
        r = api.brief(tmp_path / "fresh")
        assert not r.is_live_project
        assert "awf_init" in r.setup_hint
        assert "awf_open_project_setup_form" in r.setup_hint
        assert r.setup_hint in r.text
        assert r.phase == "init"

    def test_configured_but_never_run_is_new(self, tmp_path):
        project = _setup_project(tmp_path)
        _add_pipeline(project)  # configured, but done/ is empty
        r = api.brief(project)
        assert not r.is_live_project
        assert "setup" in r.text.lower()

    def test_empty_project_no_crash(self, tmp_path):
        project = _setup_project(tmp_path)  # bare .agentic/
        r = api.brief(project)
        assert "## Tool map" in r.text
        assert "## What's next" in r.text
        assert r.text.startswith("# awf brief —")


# ─── Word budget + determinism + JSON ────────────────────────────────────


def _mask_date(text: str) -> str:
    return re.sub(r"^- awf: .*date: \d{4}-\d{2}-\d{2}$", "- awf: DATE-MASKED",
                  text, flags=re.M)


class TestWordBudget:
    def test_live_card_under_budget(self, live_project):
        r = api.brief(live_project)
        assert len(r.text.split()) <= MAX_WORDS

    def test_new_card_under_budget(self, tmp_path):
        r = api.brief(tmp_path / "fresh")
        assert len(r.text.split()) <= MAX_WORDS

    def test_changelog_capped(self):
        section = latest_changelog()
        if section:
            assert len(section.splitlines()) <= 10


class TestDeterminism:
    def test_two_calls_identical_except_date(self, live_project):
        _make_active_todo(live_project)
        a = api.brief(live_project)
        b = api.brief(live_project)
        assert _mask_date(a.text) == _mask_date(b.text)

    def test_new_project_deterministic(self, tmp_path):
        a = api.brief(tmp_path / "fresh")
        b = api.brief(tmp_path / "fresh")
        assert _mask_date(a.text) == _mask_date(b.text)


class TestJson:
    def test_as_dict_roundtrip(self, live_project):
        r = api.brief(live_project)
        payload = json.loads(json.dumps(r.as_dict(), ensure_ascii=False))
        assert payload["text"] == r.text
        assert payload["phase"] == r.phase
        assert payload["tool_map"] == r.tool_map

    def test_cli_json_flag(self, live_project, capsys):
        from awf import cmd_brief

        args = type("A", (), {"project_dir": str(live_project), "json": True})()
        rc = cmd_brief.run(args)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["project"] == "proj"

    def test_cli_text_flag(self, live_project, capsys):
        from awf import cmd_brief

        args = type("A", (), {"project_dir": str(live_project), "json": False})()
        rc = cmd_brief.run(args)
        assert rc == 0
        assert capsys.readouterr().out.startswith("# awf brief —")

    def test_cli_nonexistent_dir_no_crash(self, tmp_path, capsys):
        """Empty/nonexistent project: no crash, header + new + map."""
        from awf import cmd_brief

        args = type("A", (), {"project_dir": str(tmp_path / "nope"), "json": False})()
        rc = cmd_brief.run(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("# awf brief —")
        assert "## Tool map" in out


# ─── Entry points: CLI subcommand + MCP tool ─────────────────────────────


class TestEntryPoints:
    def test_cli_subcommand_registered(self):
        from awf import cli

        _parser, sub = cli._build_parser()
        assert "brief" in sub.choices

    def test_mcp_tool_registered(self):
        reg = tool_registry()
        assert "awf_brief" in reg["mcp"]  # parsed from server.py

    def test_mcp_tool_ok(self, live_project):
        from agent_workflow_ui.tools import awf as awf_tools

        result = asyncio.run(awf_tools.awf_brief(project_dir=str(live_project)))
        assert result["status"] == "ok"
        assert result["text"].startswith("# awf brief —")
        assert result["is_live_project"] is True

    def test_mcp_tool_new_project(self, tmp_path):
        from agent_workflow_ui.tools import awf as awf_tools

        result = asyncio.run(
            awf_tools.awf_brief(project_dir=str(tmp_path / "fresh"))
        )
        assert result["status"] == "ok"
        assert result["is_live_project"] is False

    def test_mcp_tool_nonexistent_dir_no_crash(self, tmp_path):
        from agent_workflow_ui.tools import awf as awf_tools

        result = asyncio.run(
            awf_tools.awf_brief(project_dir=str(tmp_path / "nope"))
        )
        assert result["status"] == "ok"
        assert result["is_live_project"] is False


# ─── Prompt + docs updates ───────────────────────────────────────────────


class TestPromptAndDocs:
    @pytest.mark.parametrize("rel", SUPERVISOR_TEMPLATE_COPIES)
    def test_supervisor_md_has_brief_line(self, rel):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "awf brief" in text
        assert "New session or lost context" in text

    def test_usage_en_mentions_brief(self):
        text = (REPO_ROOT / "USAGE.md").read_text(encoding="utf-8")
        assert "`awf_brief`" in text

    def test_usage_ru_mentions_brief(self):
        text = (REPO_ROOT / "USAGE.ru.md").read_text(encoding="utf-8")
        assert "`awf_brief`" in text

    def test_tool_map_shipped_with_package_data(self):
        data_file = Path(brief_mod.__file__).parent / "data" / "tool_map.yaml"
        assert data_file.is_file()

    def test_recovery_shipped_with_package_data(self):
        assert load_recovery() != ""
