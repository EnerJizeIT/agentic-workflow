"""ORCH M5.1 (TODO-0143): role candidate isolated in the draft area.

Strategy ORCH-06: «кандидат изолирован до проверки», «без автоматической
перезаписи role .md». Invariants pinned here:

- ``add_role(draft=True)`` writes ``.agentic/roles/draft/<slug>.md``; the
  live ``roles/*.md``, config and the pipeline are not touched
  (test_draft_does_not_touch_live_roles);
- adopt is an EXPLICIT refusal on a slug conflict with a live role
  (test_adopt_refuses_name_conflict); without a conflict it moves the
  candidate into the live area;
- discard removes the candidate from the draft area (trace kept in
  ``.agentic/context/``);
- list reports name + source (skill / template / file);
- ``add_role`` without ``draft`` behaves exactly as before (back-compat).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api.roles import _read_role_files

_LIVE_ROLE = "# Live role\n\nDo live work.\n"
_CONFIG = "project_name: DraftTest\n"
_PIPELINE = "stages:\n  - role: developer\n"
_SKILL = """---
name: demo-skill
description: "A demo skill for draft tests."
---

# Demo Skill

Do the demo thing.
"""


def _make_project(base: Path, monkeypatch) -> Path:
    """Project with a live role + config + pipeline, isolated XDG home."""
    proj = base / "proj"
    ag = proj / ".agentic"
    (ag / "roles").mkdir(parents=True)
    (ag / "pipelines").mkdir()
    (ag / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (ag / "roles" / "live-role.md").write_text(_LIVE_ROLE, encoding="utf-8")
    (ag / "pipelines" / "default.yaml").write_text(_PIPELINE, encoding="utf-8")
    # global skill under a private XDG (no user's real skills)
    xdg = base / "xdg"
    skill_dir = xdg / "opencode" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return proj


def _live_snapshot(proj: Path) -> dict:
    """Snapshot of the live roles area + config + pipeline."""
    roles_dir = proj / ".agentic" / "roles"
    return {
        "roles": {
            p.name: p.read_text(encoding="utf-8") for p in roles_dir.glob("*.md")
        },
        "config": (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8"),
        "pipeline": (proj / ".agentic" / "pipelines" / "default.yaml").read_text(
            encoding="utf-8"
        ),
    }


def test_draft_does_not_touch_live_roles(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    before = _live_snapshot(proj)

    result = api.add_role(proj, "new-candidate", model="m1", draft=True)

    roles_dir = proj / ".agentic" / "roles"
    draft = roles_dir / "draft" / "new-candidate.md"
    assert draft.is_file()
    assert result.role_file == str(draft)
    content = draft.read_text(encoding="utf-8")
    assert content.startswith("<!-- awf-draft source=template")
    # isolation: the candidate is invisible to the zone analysis
    assert "new-candidate" not in _read_role_files(proj)

    assert _live_snapshot(proj) == before


def test_draft_from_skill_carries_skill_source(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)

    api.add_role(proj, "", from_skill="demo-skill", draft=True)

    draft = proj / ".agentic" / "roles" / "draft" / "demo-skill.md"
    content = draft.read_text(encoding="utf-8")
    assert content.startswith("<!-- awf-draft source=skill:demo-skill")
    assert "Do the demo thing." in content
    # the skill's own front-matter is still stripped
    assert "name: demo-skill" not in content


def test_adopt_refuses_name_conflict(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    live = proj / ".agentic" / "roles" / "live-role.md"
    api.add_role(proj, "live-role", model="m-draft", draft=True)
    draft = proj / ".agentic" / "roles" / "draft" / "live-role.md"
    assert draft.is_file()

    with pytest.raises(api.AwfApiError, match="Adopt refused"):
        api.adopt_role_draft(proj, "live-role")

    assert live.read_text(encoding="utf-8") == _LIVE_ROLE  # byte-identical
    assert draft.is_file()  # the candidate was not consumed


def test_adopt_moves_candidate_without_conflict(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    api.add_role(proj, "fresh", model="m9", draft=True)
    draft = proj / ".agentic" / "roles" / "draft" / "fresh.md"
    draft_body = draft.read_text(encoding="utf-8").split("\n", 1)[1]

    result = api.adopt_role_draft(proj, "fresh")

    live = proj / ".agentic" / "roles" / "fresh.md"
    assert result.role_file == str(live)
    assert live.is_file()
    assert not draft.exists()
    content = live.read_text(encoding="utf-8")
    assert "awf-draft" not in content  # marker stripped
    assert content == draft_body  # body preserved verbatim
    assert "fresh" in _read_role_files(proj)  # visible to zone analysis


def test_adopt_missing_draft_refused(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    api.add_role(proj, "a-candidate", model="m", draft=True)

    with pytest.raises(api.AwfApiError, match="a-candidate"):
        # the refusal names the available candidates
        api.adopt_role_draft(proj, "ghost")


def test_discard_keeps_trace_in_context(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    api.add_role(proj, "tired", model="m", draft=True)
    draft = proj / ".agentic" / "roles" / "draft" / "tired.md"
    content = draft.read_text(encoding="utf-8")

    result = api.discard_role_draft(proj, "tired")

    assert not draft.exists()
    trace = Path(result.trace_file)
    assert trace.is_file()
    assert trace.parent == proj / ".agentic" / "context"
    assert trace.read_text(encoding="utf-8") == content

    # same-day collision → -2 suffix (no overwrite of the first trace)
    api.add_role(proj, "tired", model="m", draft=True)
    result2 = api.discard_role_draft(proj, "tired")
    assert Path(result2.trace_file).name.endswith("-2.md")
    assert trace.read_text(encoding="utf-8") == content


def test_list_reports_sources(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    api.add_role(proj, "tpl-cand", model="m", draft=True)
    api.add_role(proj, "", from_skill="demo-skill", draft=True)
    (proj / ".agentic" / "roles" / "draft" / "hand-made.md").write_text(
        "# Hand-written candidate\n\nNo marker.\n", encoding="utf-8"
    )

    result = api.list_role_drafts(proj)

    by_name = {d["name"]: d for d in result.drafts}
    assert set(by_name) == {"tpl-cand", "demo-skill", "hand-made"}
    assert by_name["tpl-cand"]["source"] == "template"
    assert by_name["demo-skill"]["source"] == "skill:demo-skill"
    assert by_name["hand-made"]["source"] == "file"
    assert by_name["hand-made"]["created"] == ""


def test_add_role_back_compat_unchanged(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)

    result = api.add_role(proj, "plain", model="m1", description="a role")

    live = proj / ".agentic" / "roles" / "plain.md"
    assert result.role_file == str(live)
    assert live.is_file()
    assert "awf-draft" not in live.read_text(encoding="utf-8")
    assert not (proj / ".agentic" / "roles" / "draft").exists()


def test_draft_refuses_existing_candidate_without_force(tmp_path, monkeypatch):
    proj = _make_project(tmp_path, monkeypatch)
    api.add_role(proj, "dup", model="m1", draft=True)
    draft = proj / ".agentic" / "roles" / "draft" / "dup.md"
    first = draft.read_text(encoding="utf-8")

    with pytest.raises(api.AwfApiError, match="already exists"):
        api.add_role(proj, "dup", model="m2", draft=True)
    assert draft.read_text(encoding="utf-8") == first

    api.add_role(proj, "dup", model="m2", draft=True, force=True)
    assert "m2" in draft.read_text(encoding="utf-8")
