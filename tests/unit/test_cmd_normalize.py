"""Unit tests for awf.cmd_normalize."""
from argparse import Namespace
from pathlib import Path

import yaml

from awf import cmd_normalize


class TestCmdNormalize:

    def test_marks_normalize_needed(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        args = Namespace(project_dir=str(project_dir), check_drift=False)
        ret = cmd_normalize.run(args)

        assert ret == 0
        target = project_dir / ".agentic" / "state" / "needs_normalize.yaml"
        assert target.exists()
        parsed = yaml.safe_load(target.read_text())
        assert parsed["needed"] is True
        assert parsed["trigger"] == "manual"

    def test_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        args = Namespace(project_dir=str(project_dir), check_drift=False)
        cmd_normalize.run(args)

        state_dir = project_dir / ".agentic" / "state"
        assert state_dir.is_dir()


class TestCheckDrift:

    def test_no_skills_dir(self, tmp_path: Path, capsys) -> None:
        project_dir = tmp_path / "proj"
        (project_dir / ".agentic" / "skills").mkdir(parents=True, exist_ok=True)
        skills_dir = project_dir / ".agentic" / "skills"

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 0

    def test_no_skills_dir_at_all(self, tmp_path: Path, capsys) -> None:
        project_dir = tmp_path / "proj"
        (project_dir / ".agentic").mkdir(parents=True)
        skills_dir = project_dir / ".agentic" / "skills"

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 0
        captured = capsys.readouterr()
        assert "nothing to check" in captured.out

    def test_no_frontmatter_up_to_date(self, tmp_path: Path, capsys) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "custom-skill.md").write_text("# Custom Skill\nContent here.")

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 0
        captured = capsys.readouterr()
        assert "custom-skill.md" in captured.out
        assert "(no frontmatter)" in captured.out

    def test_matching_sha_up_to_date(self, tmp_path: Path, capsys, monkeypatch) -> None:
        global_dir = tmp_path / "global_skills"
        global_dir.mkdir()
        global_file = global_dir / "test-skill.md"
        global_file.write_text("# Test Skill\nContent.")

        skills_dir = tmp_path / "local_skills"
        skills_dir.mkdir()
        sha = cmd_normalize._sha256(global_file)
        fm = {
            "derived_from_global": True,
            "global_path": str(global_file),
            "global_sha": sha,
        }
        content = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# Test Skill\nContent."
        (skills_dir / "test-skill.md").write_text(content)

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 0
        captured = capsys.readouterr()
        assert "Up to date" in captured.out

    def test_mismatched_sha_drifted(self, tmp_path: Path, capsys, monkeypatch) -> None:
        global_dir = tmp_path / "global_skills"
        global_dir.mkdir()
        global_file = global_dir / "test-skill.md"
        global_file.write_text("# Test Skill\nOld content.")

        skills_dir = tmp_path / "local_skills"
        skills_dir.mkdir()
        old_sha = cmd_normalize._sha256(global_file)
        fm = {
            "derived_from_global": True,
            "global_path": str(global_file),
            "global_sha": old_sha,
        }
        content = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# Test Skill\nOld."
        (skills_dir / "test-skill.md").write_text(content)

        global_file.write_text("# Test Skill\nNew content.")

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 1
        captured = capsys.readouterr()
        assert "Drifted" in captured.out

    def test_missing_global_file(self, tmp_path: Path, capsys, monkeypatch) -> None:
        skills_dir = tmp_path / "local_skills"
        skills_dir.mkdir()
        fake_global = str(tmp_path / "nonexistent" / "skill.md")
        sha = "abc123"
        fm = {
            "derived_from_global": True,
            "global_path": fake_global,
            "global_sha": sha,
        }
        content = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# Skill"
        (skills_dir / "orphan-skill.md").write_text(content)

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 1
        captured = capsys.readouterr()
        assert "Missing global" in captured.out

    def test_mixed_results(self, tmp_path: Path, capsys, monkeypatch) -> None:
        global_dir = tmp_path / "global_skills"
        global_dir.mkdir()
        global_file = global_dir / "stable-skill.md"
        global_file.write_text("# Stable\nContent.")

        skills_dir = tmp_path / "local_skills"
        skills_dir.mkdir()

        # Up-to-date skill
        sha = cmd_normalize._sha256(global_file)
        fm = {
            "derived_from_global": True,
            "global_path": str(global_file),
            "global_sha": sha,
        }
        content = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# Stable"
        (skills_dir / "stable-skill.md").write_text(content)

        # No frontmatter skill
        (skills_dir / "custom-skill.md").write_text("# Custom")

        # Drifted skill
        drifted_global = global_dir / "drifted-skill.md"
        drifted_global.write_text("# Drifted\nNew.")
        old_sha = "0000000000000000"
        fm2 = {
            "derived_from_global": True,
            "global_path": str(drifted_global),
            "global_sha": old_sha,
        }
        content2 = "---\n" + yaml.safe_dump(fm2, sort_keys=False) + "---\n# Drifted"
        (skills_dir / "drifted-skill.md").write_text(content2)

        ret = cmd_normalize._check_drift(skills_dir)
        assert ret == 1
        captured = capsys.readouterr()
        assert "Up to date" in captured.out
        assert "Drifted" in captured.out


class TestParseFrontmatter:

    def test_valid_frontmatter(self) -> None:
        content = "---\nkey: value\n---\nBody text"
        fm, body = cmd_normalize._parse_frontmatter(content)
        assert fm == {"key": "value"}
        assert body == "Body text"

    def test_no_frontmatter(self) -> None:
        content = "# No frontmatter\nJust body."
        fm, body = cmd_normalize._parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_incomplete_frontmatter(self) -> None:
        content = "---\nkey: value"
        fm, body = cmd_normalize._parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_frontmatter(self) -> None:
        content = "---\n---\nBody"
        fm, body = cmd_normalize._parse_frontmatter(content)
        assert fm == {}
        assert body == content


class TestSha256:

    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert cmd_normalize._sha256(f) == cmd_normalize._sha256(f)

    def test_different_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert cmd_normalize._sha256(f1) != cmd_normalize._sha256(f2)
