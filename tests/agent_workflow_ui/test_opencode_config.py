"""Tests for opencode_config.py: model discovery, custom roles save/delete/scan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_workflow_ui.opencode_config import (
    _slugify,
    delete_custom_role,
    save_custom_role,
    scan_global_roles,
    scan_global_skills,
)

from agent_workflow_ui import opencode_config


@pytest.fixture
def isolated_roles_dir(tmp_path, monkeypatch):
    """Redirect GLOBAL_ROLES_DIR to tmp_path/roles for test isolation.

    Directory is pre-created so tests can write files directly.
    """
    roles = tmp_path / "roles"
    roles.mkdir(parents=True)
    monkeypatch.setattr(opencode_config, "GLOBAL_ROLES_DIR", roles)
    return roles


@pytest.fixture
def isolated_skills_dir(tmp_path, monkeypatch):
    """BD-27: redirect Path.home() so scan_global_skills finds test fixtures.

    Returns the skills/ directory — tests write SKILL.md files into
    subdirectories (e.g. skills/system-analyst/SKILL.md).
    """
    fake_home = tmp_path / "home"
    skills_root = fake_home / ".config" / "opencode" / "skills"
    skills_root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return skills_root


# ── _slugify ──────────────────────────────────────────────────────────────────


def test_slugify_basic():
    assert _slugify("Worker") == "worker"
    assert _slugify("Backend Developer") == "backend-developer"
    assert _slugify("ML_Engineer") == "ml_engineer"


def test_slugify_special_chars():
    # Note: slugify does NOT collapse consecutive separators (each non-allowed
    # char becomes its own '-'). strip("-") removes only leading/trailing.
    assert _slugify("C++ Dev") == "c---dev"
    assert _slugify("Data Scientist (Senior)") == "data-scientist--senior"


def test_slugify_empty():
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify("")
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify("   ")
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify("---")


def test_slugify_unicode():
    # Cyrillic is transliterated, other non-ASCII → dash then strip.
    assert _slugify("Разработчик") == "razrabotchik"
    assert _slugify("dev-разработчик") == "dev-razrabotchik"
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify("汉字")


def test_slugify_cyrillic():
    """Cyrillic names are transliterated to Latin."""
    assert _slugify("Аудитор") == "auditor"
    assert _slugify("Системный аналитик") == "sistemnyy-analitik"
    assert _slugify("привет-world") == "privet-world"
    assert _slugify("Журналист") == "zhurnalist"
    assert _slugify("Язык") == "yazyk"


# ── save_custom_role ─────────────────────────────────────────────────────────


def test_save_custom_role_agent(isolated_roles_dir):
    path = save_custom_role("Backend Dev", "# Backend\nYou are backend dev.", role_type="agent")
    assert path.name == "backend-dev.md"
    assert path.exists()
    assert "Backend" in path.read_text()


def test_save_custom_role_supervisor_adds_prefix(isolated_roles_dir):
    path = save_custom_role("Architect", "# Architect\n...", role_type="supervisor")
    assert path.name == "supervisor-architect.md"


def test_save_custom_role_supervisor_no_double_prefix(isolated_roles_dir):
    """If name already starts with supervisor-, no double prefix."""
    path = save_custom_role("supervisor-lead", "# Lead\n...", role_type="supervisor")
    assert path.name == "supervisor-lead.md"
    # NOT supervisor-supervisor-lead.md


def test_save_custom_role_creates_dir(tmp_path, monkeypatch):
    """Creates GLOBAL_ROLES_DIR if missing (uses raw fixture without pre-create)."""
    roles = tmp_path / "fresh_roles"
    assert not roles.exists()
    monkeypatch.setattr(opencode_config, "GLOBAL_ROLES_DIR", roles)
    save_custom_role("x", "content")
    assert roles.exists()


def test_save_custom_role_overwrites(isolated_roles_dir):
    save_custom_role("dev", "v1")
    save_custom_role("dev", "v2")
    path = isolated_roles_dir / "dev.md"
    assert path.read_text() == "v2"


def test_save_custom_role_empty_name_raises(isolated_roles_dir):
    """Empty name raises ValueError — no silent 'unnamed' fallback."""
    with pytest.raises(ValueError, match="no usable characters"):
        save_custom_role("", "content")


def test_save_custom_role_empty_content_raises(isolated_roles_dir):
    """Empty content raises ValueError — prevents empty role files."""
    with pytest.raises(ValueError, match="cannot be empty"):
        save_custom_role("foo", "")


def test_save_custom_role_whitespace_only_content_raises(isolated_roles_dir):
    """Whitespace-only content raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty"):
        save_custom_role("foo", "   \n\n  ")


def test_save_custom_role_valid_content_succeeds(isolated_roles_dir):
    """Non-empty content saves successfully (existing behavior unchanged)."""
    path = save_custom_role("foo", "# Hello\nContent here")
    assert path.exists()
    assert path.read_text() == "# Hello\nContent here"


# ── delete_custom_role ────────────────────────────────────────────────────────


def test_delete_custom_role_existing(isolated_roles_dir):
    save_custom_role("dev", "content")
    assert delete_custom_role("dev") is True
    assert not (isolated_roles_dir / "dev.md").exists()


def test_delete_custom_role_missing(isolated_roles_dir):
    assert delete_custom_role("nonexistent") is False


def test_delete_custom_role_path_traversal_rejected(isolated_roles_dir):
    """delete_custom_role rejects names that escape GLOBAL_ROLES_DIR."""
    save_custom_role("dev", "content")
    # Create a sibling file outside roles_dir to verify it's not touched.
    parent_file = isolated_roles_dir.parent / "passwd.md"
    parent_file.write_text("secret")

    # Relative path with ../
    assert delete_custom_role("../passwd") is False
    assert parent_file.exists(), "Path traversal should not delete sibling file"

    # Slash in name
    assert delete_custom_role("subdir/dev") is False
    # Backslash
    assert delete_custom_role("subdir\\dev") is False
    # Bare ..
    assert delete_custom_role("..") is False


# ── scan_global_roles ──────────────────────────────────────────────────────────


def test_scan_global_roles_empty(isolated_roles_dir):
    sup, agents = scan_global_roles()
    assert sup == []
    assert agents == []


def test_scan_global_roles_classifies(isolated_roles_dir):
    (isolated_roles_dir / "worker.md").write_text("# Worker\ntext")
    (isolated_roles_dir / "supervisor-strict.md").write_text("# Strict Supervisor\n...")
    (isolated_roles_dir / "reviewer.md").write_text("# Reviewer\n...")

    sup, agents = scan_global_roles()
    sup_ids = [s["id"] for s in sup]
    agent_ids = [a["id"] for a in agents]

    assert sup_ids == ["supervisor-strict"]
    assert set(agent_ids) == {"worker", "reviewer"}


def test_scan_global_roles_extracts_title(isolated_roles_dir):
    (isolated_roles_dir / "worker.md").write_text("# My Worker Role\nbody")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "My Worker Role"


def test_scan_global_roles_fallback_to_filename(isolated_roles_dir):
    """Plain text without H1 heading or frontmatter → title falls back to filename stem.

    BD-11: previously took first line verbatim, which produced "---" titles for
    files with YAML frontmatter.
    """
    (isolated_roles_dir / "worker.md").write_text("no heading here\nmore lines")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "worker"


def test_scan_global_roles_yaml_frontmatter_name(isolated_roles_dir):
    """BD-11: title comes from `name:` field in YAML frontmatter."""
    (isolated_roles_dir / "worker.md").write_text("---\nname: auditor\n---\ntest")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "auditor"


def test_scan_global_roles_yaml_frontmatter_quoted_name(isolated_roles_dir):
    """BD-11: quoted YAML name value is unquoted."""
    (isolated_roles_dir / "worker.md").write_text('---\nname: "My Cool Agent"\n---\nbody')
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "My Cool Agent"


def test_scan_global_roles_yaml_frontmatter_no_name_falls_to_h1(isolated_roles_dir):
    """BD-11: frontmatter without `name:` → look for H1 in body."""
    (isolated_roles_dir / "worker.md").write_text("---\nfoo: bar\n---\n# Real Title\nbody")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "Real Title"


def test_scan_global_roles_yaml_frontmatter_empty_name_to_filename(isolated_roles_dir):
    """BD-11: frontmatter with no name and no H1 → filename stem."""
    (isolated_roles_dir / "worker.md").write_text("---\nfoo: bar\n---\njust body")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "worker"


def test_scan_global_roles_strips_heading_marker(isolated_roles_dir):
    """First-line title has leading '# ' stripped."""
    (isolated_roles_dir / "worker.md").write_text("# My Worker\nbody")
    _, agents = scan_global_roles()
    assert agents[0]["title"] == "My Worker"


def test_scan_global_roles_unreadable_file_skipped(isolated_roles_dir):
    """Corrupt/unreadable file doesn't crash scan."""
    (isolated_roles_dir / "broken.md").write_text("# OK\nbody")
    # Make unreadable (best effort — skip if chmod doesn't apply, e.g. root).
    (isolated_roles_dir / "broken.md").chmod(0o000)
    try:
        sup, agents = scan_global_roles()
        # Either silently skipped or returns with filename fallback
        assert isinstance(sup, list)
        assert isinstance(agents, list)
    finally:
        (isolated_roles_dir / "broken.md").chmod(0o644)


def test_scan_global_roles_returns_dicts_with_keys(isolated_roles_dir):
    (isolated_roles_dir / "worker.md").write_text("# Worker\n...")
    _, agents = scan_global_roles()
    a = agents[0]
    assert set(a.keys()) == {"id", "title", "filename"}
    assert a["id"] == "worker"
    assert a["filename"] == "worker.md"


# ── read_opencode_models / read_available_models ──────────────────────────


def test_read_available_models_missing_file(tmp_path, monkeypatch):
    """Returns empty list when opencode.json doesn't exist."""
    # Mock opencode CLI to return empty (force fallback to opencode.json)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("R",(),{"returncode":1,"stdout":"","stderr":""})())
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert opencode_config.read_available_models() == []


def test_read_available_models_parses(monkeypatch, tmp_path):
    # Mock opencode CLI to return empty (force fallback to opencode.json)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("R",(),{"returncode":1,"stdout":"","stderr":""})())
    """Extracts provider/model pairs from opencode.json."""
    fake_home = tmp_path / "fake_home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(json.dumps({
        "provider": {
            "anthropic": {"models": {"claude-3.5": {}, "claude-3.7": {}}},
            "openai": {"models": ["gpt-4", "gpt-4.1"]},
        },
        "model": "anthropic/claude-3.5",
    }))
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    models = opencode_config.read_available_models()
    assert "anthropic/claude-3.5" in models
    assert "anthropic/claude-3.7" in models
    assert "openai/gpt-4" in models
    assert "openai/gpt-4.1" in models


def test_read_available_models_invalid_json(tmp_path, monkeypatch):
    """Returns empty list on JSON parse error."""
    # Mock opencode CLI to return empty (force fallback to opencode.json)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("R",(),{"returncode":1,"stdout":"","stderr":""})())
    fake_home = tmp_path / "fake_home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text("{not valid json")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert opencode_config.read_available_models() == []


# ── read_recent_models ────────────────────────────────────────────────────────


def test_read_recent_models_no_db(tmp_path, monkeypatch):
    """Returns empty list when opencode.db doesn't exist."""
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert opencode_config.read_recent_models() == []


def test_read_recent_models_reads_sqlite(tmp_path, monkeypatch):
    """Reads distinct models from opencode SQLite session table."""
    import sqlite3

    fake_home = tmp_path / "fake_home"
    share_dir = fake_home / ".local" / "share" / "opencode"
    share_dir.mkdir(parents=True)
    db_path = share_dir / "opencode.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            model TEXT,
            time_created INTEGER
        )
    """)
    sessions = [
        ("s1", json.dumps({"providerID": "anthropic", "id": "claude-3.5"}), 1000),
        ("s2", json.dumps({"providerID": "anthropic", "id": "claude-3.7"}), 2000),
        ("s3", json.dumps({"providerID": "anthropic", "id": "claude-3.5"}), 3000),
        ("s4", json.dumps({"providerID": "openai", "id": "gpt-4"}), 4000),
        ("s5", None, 5000),
    ]
    conn.executemany("INSERT INTO session VALUES (?, ?, ?)", sessions)
    conn.commit()
    conn.close()

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    recent = opencode_config.read_recent_models(limit=10)
    # claude-3.5 has two sessions — appears once (deduped).
    assert "anthropic/claude-3.5" in recent
    assert "anthropic/claude-3.7" in recent
    assert "openai/gpt-4" in recent
    # No duplicates
    assert len(recent) == len(set(recent))


def test_read_recent_models_respects_limit(tmp_path, monkeypatch):
    """Limit caps the result count."""
    import sqlite3

    fake_home = tmp_path / "fake_home"
    share_dir = fake_home / ".local" / "share" / "opencode"
    share_dir.mkdir(parents=True)
    db_path = share_dir / "opencode.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, model TEXT, time_created INTEGER)")
    for i in range(5):
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?)",
            (f"s{i}", json.dumps({"providerID": "p", "id": f"m{i}"}), i),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    recent = opencode_config.read_recent_models(limit=2)
    assert len(recent) == 2


def test_read_recent_models_closes_connection_on_error(tmp_path, monkeypatch):
    """SQL error during query does not leak the sqlite3 connection.

    Regression: previously conn.close() was inside the try block — any
    exception during conn.execute(...) would skip close(). Now wrapped
    in try/finally. We can't monkey-patch Connection.close directly
    (it's read-only in CPython), so we wrap at the sqlite3.connect
    level with a proxy that records close() calls.
    """
    import sqlite3

    fake_home = tmp_path / "fake_home"
    share_dir = fake_home / ".local" / "share" / "opencode"
    share_dir.mkdir(parents=True)
    db_path = share_dir / "opencode.db"

    # Create DB without the `session` table — query will raise
    # sqlite3.OperationalError ("no such table").
    sqlite3.connect(str(db_path)).close()

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    closed_flags: list[bool] = []
    real_connect = sqlite3.connect

    class _ConnProxy:
        """Proxy that records close() calls and forwards everything else."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            closed_flags.append(True)
            return self._real.close()

    def tracking_connect(*args, **kwargs):
        return _ConnProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    result = opencode_config.read_recent_models(limit=10)
    # Empty result on error.
    assert result == []
    # Connection was still closed despite the SQL error.
    assert closed_flags, "sqlite3.Connection.close() was never called — leak"


# ── BD-27: scan_global_skills ────────────────────────────────────────────────


def test_scan_global_skills_empty(isolated_skills_dir):
    """No skills → empty list."""
    assert scan_global_skills() == []


def test_scan_global_skills_finds_skill_md(isolated_skills_dir):
    """Skill with SKILL.md is discovered with full content."""
    skill_dir = isolated_skills_dir / "developer"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Developer\n\nImplement features.\n")

    skills = scan_global_skills()
    assert len(skills) == 1
    s = skills[0]
    assert s["id"] == "developer"
    assert s["title"] == "Developer"
    assert "Implement features" in s["content"]
    assert s["path"].endswith("SKILL.md")


def test_scan_global_skills_extracts_description_from_frontmatter(isolated_skills_dir):
    """Description from YAML frontmatter takes precedence over body text."""
    skill_dir = isolated_skills_dir / "qa-review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: QA Review\ndescription: \"Find bugs and write tests\"\n---\n# QA\nbody"
    )

    skills = scan_global_skills()
    assert len(skills) == 1
    assert "Find bugs" in skills[0]["description"]


def test_scan_global_skills_fallback_to_body_first_line(isolated_skills_dir):
    """Without frontmatter description, first non-empty body line is used."""
    skill_dir = isolated_skills_dir / "minimal"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Minimal\nThis is the description.\nMore body.")

    skills = scan_global_skills()
    assert skills[0]["description"] == "This is the description."


def test_scan_global_skills_skips_dirs_without_skill_md(isolated_skills_dir):
    """Directories without SKILL.md are skipped silently."""
    (isolated_skills_dir / "no-skill").mkdir()
    (isolated_skills_dir / "no-skill" / "README.md").write_text("not a skill")

    skill_dir = isolated_skills_dir / "has-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Has Skill\n")

    skills = scan_global_skills()
    assert len(skills) == 1
    assert skills[0]["id"] == "has-skill"


def test_scan_global_skills_returns_sorted_by_id(isolated_skills_dir):
    """Skills are returned alphabetically by directory name."""
    for name in ["zebra", "alpha", "monkey"]:
        d = isolated_skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}\n")

    skills = scan_global_skills()
    ids = [s["id"] for s in skills]
    assert ids == ["alpha", "monkey", "zebra"]
