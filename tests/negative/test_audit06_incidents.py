"""AUD06-06: path traversal via role slug in add_role (public MCP input).

The MCP tool ``awf_add_role`` documents ``name`` as a "Role slug" but never
validates it. ``add_role`` used to do ``roles_dir / f"{role_name}.md"``
verbatim, so ``add_role(p, "../../x")`` wrote ``<parent-of-.agentic>/x.md``
— outside .agentic/roles/. The slug must be validated at the API boundary;
the resolve()-crosscheck is defense in depth.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + roles/)."""
    api.init_project(tmp_git_repo, project_name="Traversal")
    return tmp_git_repo


class TestAddRoleSlugValidation:
    """AUD06-06: a slug arriving from public input must not escape roles/."""

    def test_dotdot_slug_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "../x")
        # nothing created outside .agentic/roles/
        assert not (awf_project.parent / "x.md").exists()
        roles = awf_project / ".agentic" / "roles"
        assert not (roles / "..").joinpath("x.md").exists()
        # no NEW files anywhere under the repo except the expected roles dir
        created = [p for p in awf_project.rglob("*.md") if p.name == "x.md"]
        assert created == []

    def test_slash_slug_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "a/b")
        assert not (awf_project / ".agentic" / "roles" / "a" / "b.md").exists()

    def test_absolute_path_slug_rejected(self, awf_project):
        """Absolute names must be rejected too (Path / abs = abs)."""
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "/etc/cron.d/awf")
        assert not Path("/etc/cron.d/awf.md").exists()

    def test_empty_slug_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "")

    def test_whitespace_slug_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "   ")

    def test_valid_slug_still_works(self, awf_project):
        result = api.add_role(awf_project, "new-role_1")
        role_file = awf_project / ".agentic" / "roles" / "new-role_1.md"
        assert role_file.is_file()
        assert Path(result.role_file) == role_file.resolve()

    def test_uppercase_slug_normalized(self, awf_project):
        """'MyRole' is a slug with different case — normalize, don't guess."""
        result = api.add_role(awf_project, " MyRole ")
        role_file = awf_project / ".agentic" / "roles" / "myrole.md"
        assert role_file.is_file()
        assert result.role_name == "myrole"


class TestAddRoleSkillNameValidation:
    """RUN3 #3: from_skill is public input — same slug rule as role name.

    A "../../x" skill name must not read a file outside the skills roots;
    validation happens before any filesystem access.
    """

    def test_dotdot_skill_name_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "x", from_skill="../../x")

    def test_slash_skill_name_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "x", from_skill="a/b")

    def test_absolute_skill_name_rejected(self, awf_project):
        with pytest.raises(AwfApiError):
            api.add_role(awf_project, "x", from_skill="/etc/cron.d/awf")

    def test_empty_skill_name_without_role_name_rejected(self, awf_project):
        """from_skill='' is the template path — empty name errors as before."""
        with pytest.raises(AwfApiError, match="role_name is required"):
            api.add_role(awf_project, "", from_skill="")


class TestCorruptedPipelineDegrades:
    """AUD06-16: a broken pipelines/default.yaml must degrade, not traceback.

    Before the fix: non-UTF-8 bytes slipped past ``except yaml.YAMLError``
    in load_stages and crashed analyze_roles with a raw
    UnicodeDecodeError (MCP: "Unexpected UnicodeDecodeError").
    """

    @pytest.fixture
    def awf_project(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="Roles")
        roles = tmp_git_repo / ".agentic" / "roles"
        (roles / "developer.md").write_text(
            "# developer\n\nWrites code.\n", encoding="utf-8"
        )
        return tmp_git_repo

    def _corrupt_pipeline(self, project, payload: bytes):
        pipe = project / ".agentic" / "pipelines" / "default.yaml"
        pipe.parent.mkdir(parents=True, exist_ok=True)
        pipe.write_bytes(payload)

    def test_non_utf8_pipeline_degrades(self, awf_project):
        self._corrupt_pipeline(awf_project, b"\xff\xfe\x00stages: [plan]")
        result = api.analyze_roles(awf_project, dry_run=True)
        assert result.dry_run is True
        assert isinstance(result.overlaps, list)

    def test_truncated_yaml_pipeline_degrades(self, awf_project):
        self._corrupt_pipeline(
            awf_project, b"stages:\n  - name: plan\n    role: [unclosed"
        )
        result = api.analyze_roles(awf_project, dry_run=True)
        assert result.dry_run is True

    def test_read_pipeline_roles_returns_empty_on_corrupt(self, awf_project):
        self._corrupt_pipeline(awf_project, b"\xff\xfe\x00garbage")
        from awf.api.roles import _read_pipeline_roles

        assert _read_pipeline_roles(awf_project, {}) == []

    def test_load_stages_itself_degrades_on_non_utf8(self, awf_project):
        """Systemic layer: load_stages must not raise on corrupt bytes —
        this protects the pipeline engine and start_pipeline, not just
        analyze_roles (whose guard would mask a load_stages regression)."""
        self._corrupt_pipeline(awf_project, b"\xff\xfe\x00stages: [plan]")
        from awf.pipeline import load_stages

        pipe = awf_project / ".agentic" / "pipelines" / "default.yaml"
        assert load_stages(pipe) == []

    def test_non_utf8_role_file_is_skipped(self, awf_project):
        """A corrupted role .md must not kill the whole analysis."""
        roles = awf_project / ".agentic" / "roles"
        (roles / "corrupt.md").write_bytes(b"\xff\xfe\x00bad")
        from awf.api.roles import _read_role_files

        files = _read_role_files(awf_project)
        assert "developer" in files
        assert "corrupt" not in files
