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
