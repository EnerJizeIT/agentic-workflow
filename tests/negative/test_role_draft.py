"""ORCH M5.1 (TODO-0143) + M5.3 (TODO-0145): draft area + adopt-normalization.

Strategy ORCH-06: «кандидат изолирован до проверки», «без автоматической
перезаписи role .md», «инкрементальная нормализация решает конфликты имени и
пересечения без перезапуска setup-фазы». Invariants pinned here:

- ``add_role(draft=True)`` writes ``.agentic/roles/draft/<slug>.md``; the
  live ``roles/*.md``, config and the pipeline are not touched
  (test_draft_does_not_touch_live_roles);
- adopt is an EXPLICIT refusal on a slug conflict with a live role, naming
  the «different slug» resolution (test_adopt_refuses_name_conflict);
  without a conflict it moves the candidate into the live area;
- M5.3: the adopt is ALSO normalization — ``analyze_roles_core`` re-runs and
  the BD-31 disambiguation addenda are refreshed. Only the BD-31 blocks of
  role files change (bodies preserved byte-for-byte, idempotent); config and
  the pipeline are NOT touched and the setup phase is not re-run; a broken
  analysis degrades to a reported error without undoing the adopt;
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
# M5.3: a verify-zone role that overlaps (verify family) with the
# candidate slug "auditor" (also verify-zone) — drives the normalization.
_QA_ROLE = "# QA\nVerifies the implementation against the requirements.\n"
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


def _role(proj: Path, slug: str) -> str:
    return (proj / ".agentic" / "roles" / f"{slug}.md").read_text(encoding="utf-8")


def _make_overlap_project(base: Path, monkeypatch) -> Path:
    """Project where the live role ``qa`` (verify-zone) overlaps the
    candidate slug ``auditor`` (verify-zone): the zone families collide,
    so adopt-normalization must disambiguate both (M5.3)."""
    proj = base / "overlap"
    ag = proj / ".agentic"
    (ag / "roles").mkdir(parents=True)
    (ag / "pipelines").mkdir()
    (ag / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (ag / "roles" / "qa.md").write_text(_QA_ROLE, encoding="utf-8")
    (ag / "pipelines" / "default.yaml").write_text(
        'name: "default"\nstages:\n'
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "work"\n    role: "worker"\n'
        '  - name: "verify"\n    role: "supervisor"\n',
        encoding="utf-8",
    )
    xdg = base / "xdg"
    (xdg / "opencode" / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return proj


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

    with pytest.raises(api.AwfApiError, match="Adopt refused") as exc:
        api.adopt_role_draft(proj, "live-role")
    # M5.3: the refusal names the resolution — adopt under a different slug.
    assert "different slug" in str(exc.value)

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
    # M5.3: the adopt runs normalization — the body is preserved as a byte
    # prefix and the BD-31 addendum is appended (no body duplication).
    assert content.startswith(draft_body.rstrip())
    assert content.count("## BD-31") <= 1
    assert "fresh" in _read_role_files(proj)  # visible to zone analysis
    # The normalization report is present and clean (no error).
    assert "error" not in result.normalization


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


# ─── ORCH M5.3: the adopt is also the normalization step ─────────────────
#
# Adopt re-runs zone analysis (analyze_roles_core) and refreshes the BD-31
# disambiguation addenda. Only the BD-31 blocks of role files change (bodies
# preserved byte-for-byte, idempotent); config and the active pipeline are
# NOT touched and the setup phase is not re-run. A broken analysis degrades
# to a reported error without undoing the adopt.


def test_adopt_normalizes_zone_overlap(tmp_path, monkeypatch):
    """(а) Zone overlap → addenda via analyze_roles_core: both the live
    ``qa`` and the adopted ``auditor`` (verify family) get the BD-31 block,
    their bodies preserved byte-for-byte; config + pipeline untouched
    ((c)+(d): no setup re-run, no pipeline rewrite)."""
    proj = _make_overlap_project(tmp_path, monkeypatch)
    qa_before = _role(proj, "qa")
    config_before = (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    pipe_before = (proj / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8")

    api.add_role(proj, "auditor", model="m-audit", draft=True)
    result = api.adopt_role_draft(proj, "auditor")

    # The role is live and available; normalization did not fail.
    auditor = proj / ".agentic" / "roles" / "auditor.md"
    assert auditor.is_file()
    assert "error" not in result.normalization

    # The verify-zone overlap (qa vs auditor) is reported.
    pairs = {frozenset((o["role_a"], o["role_b"])) for o in result.normalization["overlaps"]}
    assert frozenset(("qa", "auditor")) in pairs, result.normalization["overlaps"]

    # Both roles carry the BD-31 addendum; bodies preserved byte-for-byte.
    auditor_text = auditor.read_text(encoding="utf-8")
    assert "## BD-31" in auditor_text and "qa" in auditor_text
    qa_after = _role(proj, "qa")
    assert qa_after.startswith(qa_before.rstrip()), "qa.md body must be preserved"
    assert qa_after.count("## BD-31") == 1, "exactly one BD-31 block (no duplication)"
    assert "auditor" in qa_after, "the qa addendum disambiguates against auditor"

    # (c)+(d): setup phase not re-run — config and pipeline byte-identical.
    assert (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8") == config_before
    assert (proj / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8") == pipe_before


def test_adopt_normalization_is_idempotent(tmp_path, monkeypatch):
    """A second adopt re-runs the same analysis: the BD-31 block is
    replaced, never duplicated (the BD-31 marker makes it idempotent)."""
    proj = _make_overlap_project(tmp_path, monkeypatch)
    api.add_role(proj, "auditor", model="m-audit", draft=True)
    api.adopt_role_draft(proj, "auditor")
    qa_once = _role(proj, "qa")
    assert qa_once.count("## BD-31") == 1

    api.add_role(proj, "auditor2", model="m2", draft=True)
    result2 = api.adopt_role_draft(proj, "auditor2")
    qa_twice = _role(proj, "qa")

    assert qa_twice.count("## BD-31") == 1, "no BD-31 duplication on re-normalization"
    assert "error" not in result2.normalization


def test_adopt_survives_normalization_failure(tmp_path, monkeypatch):
    """A broken analysis (pipeline that fails form validation) must not
    undo the adopt: the role goes live, the error is reported in the
    result's normalization dict."""
    proj = _make_overlap_project(tmp_path, monkeypatch)
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        "stages: not-a-list\n", encoding="utf-8"
    )

    api.add_role(proj, "auditor", model="m-audit", draft=True)
    result = api.adopt_role_draft(proj, "auditor")

    assert (proj / ".agentic" / "roles" / "auditor.md").is_file(), "the adopt stands"
    assert "error" in result.normalization
